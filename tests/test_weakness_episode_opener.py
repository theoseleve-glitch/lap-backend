"""T208 — open or merge a WeaknessEpisode for a CoachingInsight.

Per ADR-005, a WeaknessEpisode is the longitudinal record ("the
driver struggles with X") that owns N CoachingInsights ("on
session Y, rule X fired"). T208 is the dedup logic: when a new
insight lands, find the OPEN episode for the same
(driver_id, library_rule_id, corner_tag) tuple — merge into it
if it exists, open a new one otherwise.

Resolved episodes are NOT candidates: a fresh occurrence after
resolution is a regression worth a new episode, not a silent
reopen.
"""

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
import uuid6
import yaml
from alembic.config import Config as AlembicConfig
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from coaching.library.rule_schema import RuleSpec
from infra.coaching_insight_persistence import persist_insights
from infra.db import make_engine, make_session_factory
from infra.orm import (
    CoachingInsight,
    DriverProfile,
    Session,
    User,
    WeaknessEpisode,
)
from infra.weakness_episode_opener import upsert_weakness_episode

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def session_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[async_sessionmaker[AsyncSession], None, None]:
    db_path = tmp_path / "we.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    yield factory


_RULE_T188_YAML = """
id: "br.late_braking.apex_speed_loss"
version: 1
category: technique
subcategory: late_braking
difficulty: beginner
detection:
  signal: apex_speed
  condition: "apex_speed < reference_apex_speed * 0.93"
  context:
    must_have_channels: ["brake"]
    min_corner_priority: medium
  evidence_required:
    min_occurrences_in_session: 3
    min_sessions_in_window: 2
    window_days: 14
severity_rule: "linear"
explanation_template:
  beginner: "b"
  intermediate: "i"
  advanced: "a"
prescription:
  drill_template_id: "drill.x"
  expected_gain_kmh: "1"
  success_condition:
    metric: "apex_speed"
    comparison: "gte"
    target_pct: 0.96
    measurement_window: "next_3_sessions"
"""


def _rule(rule_id: str = "br.late_braking.apex_speed_loss") -> RuleSpec:
    raw = yaml.safe_load(_RULE_T188_YAML)
    raw["id"] = rule_id
    return RuleSpec.model_validate(raw)


async def _seed_driver_with_session(
    db: AsyncSession, *, suffix: str = ""
) -> tuple[UUID, UUID]:
    """Returns (driver_id, session_id). Suffix lets one test create
    multiple users without email collisions."""
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    session_id = uuid6.uuid7()
    db.add(
        User(
            id=user_id,
            clerk_user_id=f"u_we{suffix}_{user_id.hex[:8]}",
            email=f"we{suffix}_{user_id.hex[:8]}@test",
        )
    )
    db.add(
        DriverProfile(id=driver_id, user_id=user_id, display_name="A")
    )
    db.add(
        Session(
            id=session_id,
            driver_id=driver_id,
            manifest={},
            status="ready",
            created_at=datetime.now(UTC),
        )
    )
    await db.commit()
    return driver_id, session_id


async def _seed_one_insight(
    db: AsyncSession,
    *,
    driver_id: UUID,
    session_id: UUID,
    corner_tag: str | None = None,
) -> CoachingInsight:
    """Persist exactly one insight via the T207 helper, then load
    it back. Uses the helper rather than crafting the row by hand
    so we exercise the same code path the production wiring will."""
    from coaching.library.rule_runner import (
        CoachingInsight as RunnerInsight,
    )

    runner_ins = RunnerInsight(
        rule_id="br.late_braking.apex_speed_loss",
        rule_version=1,
        category="technique",
        subcategory="late_braking",
        difficulty="beginner",
        explanation="X",
    )
    [insight_id] = await persist_insights(
        db,
        insights=[runner_ins],
        session_id=session_id,
        driver_id=driver_id,
        now=datetime.now(UTC),
    )
    if corner_tag is not None:
        # T207 persists corner_tag=None always; for tests that need
        # a specific tag, patch it after the fact.
        row = (
            await db.execute(
                select(CoachingInsight).where(
                    CoachingInsight.id == insight_id
                )
            )
        ).scalar_one()
        row.corner_tag = corner_tag
        await db.commit()
    return (
        await db.execute(
            select(CoachingInsight).where(CoachingInsight.id == insight_id)
        )
    ).scalar_one()


# ── opening a new episode ──────────────────────────────────────


async def test_no_existing_episode_opens_a_new_one(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        driver_id, session_id = await _seed_driver_with_session(db)
        insight = await _seed_one_insight(
            db, driver_id=driver_id, session_id=session_id
        )
        ep_id, opened_new = await upsert_weakness_episode(
            db,
            insight_row=insight,
            rule=_rule(),
            now=datetime.now(UTC),
        )
        eps = (
            await db.execute(select(WeaknessEpisode))
        ).scalars().all()
    assert opened_new is True
    assert len(eps) == 1
    assert eps[0].id == ep_id


async def test_new_episode_carries_rule_metadata(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A freshly opened episode pulls category/subcategory from
    the rule, library_rule_id from the insight, and severity from
    the persisted insight row."""
    async with session_factory() as db:
        driver_id, session_id = await _seed_driver_with_session(db)
        insight = await _seed_one_insight(
            db, driver_id=driver_id, session_id=session_id
        )
        await upsert_weakness_episode(
            db,
            insight_row=insight,
            rule=_rule(),
            now=datetime.now(UTC),
        )
        ep = (
            await db.execute(select(WeaknessEpisode))
        ).scalar_one()
    assert ep.driver_id == insight.driver_id
    assert ep.library_rule_id == "br.late_braking.apex_speed_loss"
    assert ep.category == "technique"
    assert ep.subcategory == "late_braking"
    assert ep.severity == insight.severity
    assert ep.status == "open"
    assert ep.recurrence_count == 1


async def test_new_episode_records_first_session_and_insight(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        driver_id, session_id = await _seed_driver_with_session(db)
        insight = await _seed_one_insight(
            db, driver_id=driver_id, session_id=session_id
        )
        await upsert_weakness_episode(
            db,
            insight_row=insight,
            rule=_rule(),
            now=datetime.now(UTC),
        )
        ep = (
            await db.execute(select(WeaknessEpisode))
        ).scalar_one()
    assert ep.sessions_affected == [str(session_id)]
    assert ep.linked_insights == [str(insight.id)]


# ── merging into an existing episode ──────────────────────────


async def test_existing_open_episode_increments_recurrence_count(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        driver_id, session_id_a = await _seed_driver_with_session(db)
        ins_a = await _seed_one_insight(
            db, driver_id=driver_id, session_id=session_id_a
        )
        await upsert_weakness_episode(
            db,
            insight_row=ins_a,
            rule=_rule(),
            now=datetime.now(UTC),
        )
        # Second session, same driver, same rule → merge.
        session_id_b = uuid6.uuid7()
        db.add(
            Session(
                id=session_id_b,
                driver_id=driver_id,
                manifest={},
                status="ready",
                created_at=datetime.now(UTC),
            )
        )
        await db.commit()
        ins_b = await _seed_one_insight(
            db, driver_id=driver_id, session_id=session_id_b
        )
        ep_id, opened_new = await upsert_weakness_episode(
            db,
            insight_row=ins_b,
            rule=_rule(),
            now=datetime.now(UTC),
        )
        eps = (
            await db.execute(select(WeaknessEpisode))
        ).scalars().all()
    assert opened_new is False
    assert len(eps) == 1
    assert eps[0].id == ep_id
    assert eps[0].recurrence_count == 2


async def test_merge_updates_last_observed_at(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`first_observed_at` stays pinned, `last_observed_at`
    advances. This is what the decay job (T209) reads to compute
    'days since last evidence'."""
    t0 = datetime(2026, 4, 1, tzinfo=UTC)
    t1 = t0 + timedelta(days=7)
    async with session_factory() as db:
        driver_id, session_id_a = await _seed_driver_with_session(db)
        ins_a = await _seed_one_insight(
            db, driver_id=driver_id, session_id=session_id_a
        )
        await upsert_weakness_episode(
            db, insight_row=ins_a, rule=_rule(), now=t0
        )
        session_id_b = uuid6.uuid7()
        db.add(
            Session(
                id=session_id_b, driver_id=driver_id,
                manifest={}, status="ready", created_at=t1,
            )
        )
        await db.commit()
        ins_b = await _seed_one_insight(
            db, driver_id=driver_id, session_id=session_id_b
        )
        await upsert_weakness_episode(
            db, insight_row=ins_b, rule=_rule(), now=t1
        )
        ep = (
            await db.execute(select(WeaknessEpisode))
        ).scalar_one()
    assert ep.first_observed_at.replace(tzinfo=UTC) == t0
    assert ep.last_observed_at.replace(tzinfo=UTC) == t1


async def test_merge_appends_session_id_to_sessions_affected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        driver_id, session_id_a = await _seed_driver_with_session(db)
        ins_a = await _seed_one_insight(
            db, driver_id=driver_id, session_id=session_id_a
        )
        await upsert_weakness_episode(
            db, insight_row=ins_a, rule=_rule(), now=datetime.now(UTC)
        )
        session_id_b = uuid6.uuid7()
        db.add(
            Session(
                id=session_id_b, driver_id=driver_id,
                manifest={}, status="ready",
                created_at=datetime.now(UTC),
            )
        )
        await db.commit()
        ins_b = await _seed_one_insight(
            db, driver_id=driver_id, session_id=session_id_b
        )
        await upsert_weakness_episode(
            db, insight_row=ins_b, rule=_rule(), now=datetime.now(UTC)
        )
        ep = (
            await db.execute(select(WeaknessEpisode))
        ).scalar_one()
    assert ep.sessions_affected == [
        str(session_id_a), str(session_id_b),
    ]


async def test_merge_appends_insight_id_to_linked_insights(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        driver_id, session_id_a = await _seed_driver_with_session(db)
        ins_a = await _seed_one_insight(
            db, driver_id=driver_id, session_id=session_id_a
        )
        await upsert_weakness_episode(
            db, insight_row=ins_a, rule=_rule(), now=datetime.now(UTC)
        )
        session_id_b = uuid6.uuid7()
        db.add(
            Session(
                id=session_id_b, driver_id=driver_id,
                manifest={}, status="ready",
                created_at=datetime.now(UTC),
            )
        )
        await db.commit()
        ins_b = await _seed_one_insight(
            db, driver_id=driver_id, session_id=session_id_b
        )
        await upsert_weakness_episode(
            db, insight_row=ins_b, rule=_rule(), now=datetime.now(UTC)
        )
        ep = (
            await db.execute(select(WeaknessEpisode))
        ).scalar_one()
    assert ep.linked_insights == [str(ins_a.id), str(ins_b.id)]


# ── boundaries that must NOT merge ─────────────────────────────


async def test_resolved_episode_does_not_block_new_one(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Status `resolved` means 'driver fixed it' — fresh evidence
    is a regression worth its own episode, not a stealth reopen
    of the resolved one."""
    async with session_factory() as db:
        driver_id, session_id = await _seed_driver_with_session(db)
        # Pre-existing resolved episode for the same rule.
        resolved_id = uuid6.uuid7()
        now = datetime.now(UTC)
        db.add(
            WeaknessEpisode(
                id=resolved_id,
                driver_id=driver_id,
                category="technique",
                subcategory="late_braking",
                first_observed_at=now,
                last_observed_at=now,
                recurrence_count=5,
                sessions_affected=[],
                severity="medium",
                confidence=0.5,
                status="resolved",
                resolved_at=now,
                linked_prescriptions=[],
                linked_insights=[],
                library_rule_id="br.late_braking.apex_speed_loss",
            )
        )
        await db.commit()
        insight = await _seed_one_insight(
            db, driver_id=driver_id, session_id=session_id
        )
        new_id, opened_new = await upsert_weakness_episode(
            db,
            insight_row=insight,
            rule=_rule(),
            now=datetime.now(UTC),
        )
    assert opened_new is True
    assert new_id != resolved_id


async def test_different_driver_does_not_merge(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        driver_a, session_a = await _seed_driver_with_session(
            db, suffix="_a"
        )
        ins_a = await _seed_one_insight(
            db, driver_id=driver_a, session_id=session_a
        )
        await upsert_weakness_episode(
            db, insight_row=ins_a, rule=_rule(), now=datetime.now(UTC)
        )
        # Different driver, same rule.
        driver_b, session_b = await _seed_driver_with_session(
            db, suffix="_b"
        )
        ins_b = await _seed_one_insight(
            db, driver_id=driver_b, session_id=session_b
        )
        _, opened_new = await upsert_weakness_episode(
            db, insight_row=ins_b, rule=_rule(), now=datetime.now(UTC)
        )
        eps = (
            await db.execute(select(WeaknessEpisode))
        ).scalars().all()
    assert opened_new is True
    assert len(eps) == 2


async def test_different_rule_does_not_merge(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        driver_id, session_id_a = await _seed_driver_with_session(db)
        ins_a = await _seed_one_insight(
            db, driver_id=driver_id, session_id=session_id_a
        )
        await upsert_weakness_episode(
            db, insight_row=ins_a, rule=_rule(), now=datetime.now(UTC)
        )
        # Same driver+session, different rule_id (we patch the
        # insight row directly to model it).
        ins_b = await _seed_one_insight(
            db, driver_id=driver_id, session_id=session_id_a
        )
        ins_b.rule_id = "br.lockup.pattern_detection"
        await db.commit()
        _, opened_new = await upsert_weakness_episode(
            db,
            insight_row=ins_b,
            rule=_rule(rule_id="br.lockup.pattern_detection"),
            now=datetime.now(UTC),
        )
        eps = (
            await db.execute(select(WeaknessEpisode))
        ).scalars().all()
    assert opened_new is True
    assert len(eps) == 2


async def test_different_corner_tag_does_not_merge(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Spa/T1 and Spa/Eau-Rouge are separate weaknesses even for
    the same rule — corner_tag is part of the natural identity."""
    async with session_factory() as db:
        driver_id, session_id = await _seed_driver_with_session(db)
        ins_t1 = await _seed_one_insight(
            db, driver_id=driver_id, session_id=session_id,
            corner_tag="Spa/T1",
        )
        await upsert_weakness_episode(
            db, insight_row=ins_t1, rule=_rule(), now=datetime.now(UTC)
        )
        ins_er = await _seed_one_insight(
            db, driver_id=driver_id, session_id=session_id,
            corner_tag="Spa/Eau-Rouge",
        )
        _, opened_new = await upsert_weakness_episode(
            db, insight_row=ins_er, rule=_rule(), now=datetime.now(UTC)
        )
        eps = (
            await db.execute(select(WeaknessEpisode))
        ).scalars().all()
    assert opened_new is True
    assert len(eps) == 2


async def test_same_corner_tag_merges(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Same Spa/T1 again on a different session → merge."""
    async with session_factory() as db:
        driver_id, session_a = await _seed_driver_with_session(db)
        ins_a = await _seed_one_insight(
            db, driver_id=driver_id, session_id=session_a,
            corner_tag="Spa/T1",
        )
        await upsert_weakness_episode(
            db, insight_row=ins_a, rule=_rule(), now=datetime.now(UTC)
        )
        session_b = uuid6.uuid7()
        db.add(
            Session(
                id=session_b, driver_id=driver_id,
                manifest={}, status="ready",
                created_at=datetime.now(UTC),
            )
        )
        await db.commit()
        ins_b = await _seed_one_insight(
            db, driver_id=driver_id, session_id=session_b,
            corner_tag="Spa/T1",
        )
        _, opened_new = await upsert_weakness_episode(
            db, insight_row=ins_b, rule=_rule(), now=datetime.now(UTC)
        )
        eps = (
            await db.execute(select(WeaknessEpisode))
        ).scalars().all()
    assert opened_new is False
    assert len(eps) == 1
    assert eps[0].recurrence_count == 2
