"""T210 — WeaknessEpisode recurrence counter contract.

`recurrence_count` is the number of distinct observations in
TIME — not the number of insight rows. A re-process of the same
session (Celery retry, manual replay) must not bump the counter,
even though it may create a new CoachingInsight row.

Dedup key is `session_id`: T207's persist_insights doesn't
dedup, so two retry runs of the same session produce two insight
rows with the same session_id. T208's upsert is the natural
choke-point — if the session is already in sessions_affected,
the counter stays put.

`linked_insights` is allowed to diverge from `recurrence_count`
— it's the audit trail of every insight that ever pointed at
the episode. ADR-005 §5.2 documents this split.
"""

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
import uuid6
import yaml
from alembic.config import Config as AlembicConfig
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from coaching.library.rule_runner import (
    CoachingInsight as RunnerInsight,
)
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
    db_path = tmp_path / "rec.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    yield factory


_RULE_YAML = """
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


def _rule() -> RuleSpec:
    return RuleSpec.model_validate(yaml.safe_load(_RULE_YAML))


async def _seed_driver(
    db: AsyncSession, *, suffix: str = ""
) -> UUID:
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    db.add(
        User(
            id=user_id,
            clerk_user_id=f"u_rec{suffix}_{user_id.hex[:8]}",
            email=f"rec{suffix}_{user_id.hex[:8]}@test",
        )
    )
    db.add(
        DriverProfile(id=driver_id, user_id=user_id, display_name="A")
    )
    await db.commit()
    return driver_id


async def _add_session(db: AsyncSession, driver_id: UUID) -> UUID:
    session_id = uuid6.uuid7()
    db.add(
        Session(
            id=session_id, driver_id=driver_id,
            manifest={}, status="ready", created_at=datetime.now(UTC),
        )
    )
    await db.commit()
    return session_id


async def _persist_one_and_load(
    db: AsyncSession, *, driver_id: UUID, session_id: UUID
) -> CoachingInsight:
    """Persist one insight via T207 and return the row."""
    runner = RunnerInsight(
        rule_id="br.late_braking.apex_speed_loss",
        rule_version=1,
        category="technique",
        subcategory="late_braking",
        difficulty="beginner",
        explanation="X",
    )
    [insight_id] = await persist_insights(
        db,
        insights=[runner],
        session_id=session_id,
        driver_id=driver_id,
        now=datetime.now(UTC),
    )
    return (
        await db.execute(
            select(CoachingInsight).where(
                CoachingInsight.id == insight_id
            )
        )
    ).scalar_one()


# ── the contract ──────────────────────────────────────────────


async def test_recurrence_counter_starts_at_one_on_first_observation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """First observation = first occurrence = counter == 1.
    Pinning so a future refactor doesn't accidentally start at 0
    or mistakenly leave it at None."""
    async with session_factory() as db:
        driver_id = await _seed_driver(db)
        session_id = await _add_session(db, driver_id)
        insight = await _persist_one_and_load(
            db, driver_id=driver_id, session_id=session_id
        )
        await upsert_weakness_episode(
            db, insight_row=insight, rule=_rule(),
            now=datetime.now(UTC),
        )
        ep = (
            await db.execute(select(WeaknessEpisode))
        ).scalar_one()
    assert ep.recurrence_count == 1


async def test_recurrence_counter_bumps_on_new_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A genuine re-observation (different session, same rule)
    bumps the counter — this is the happy-path increment."""
    async with session_factory() as db:
        driver_id = await _seed_driver(db)
        s1 = await _add_session(db, driver_id)
        ins1 = await _persist_one_and_load(
            db, driver_id=driver_id, session_id=s1
        )
        await upsert_weakness_episode(
            db, insight_row=ins1, rule=_rule(),
            now=datetime.now(UTC),
        )
        s2 = await _add_session(db, driver_id)
        ins2 = await _persist_one_and_load(
            db, driver_id=driver_id, session_id=s2
        )
        await upsert_weakness_episode(
            db, insight_row=ins2, rule=_rule(),
            now=datetime.now(UTC),
        )
        ep = (
            await db.execute(select(WeaknessEpisode))
        ).scalar_one()
    assert ep.recurrence_count == 2


async def test_recurrence_counter_idempotent_on_same_session_replay(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A re-process of the same session (Celery retry) creates
    a new insight row but is NOT a new observation. Counter must
    stay put — re-observation means new in time, not new row."""
    async with session_factory() as db:
        driver_id = await _seed_driver(db)
        s1 = await _add_session(db, driver_id)
        ins_a = await _persist_one_and_load(
            db, driver_id=driver_id, session_id=s1
        )
        await upsert_weakness_episode(
            db, insight_row=ins_a, rule=_rule(),
            now=datetime.now(UTC),
        )
        # Same session, same rule, fresh insight row (T207 does
        # not dedup — Celery retry models exactly this).
        ins_b = await _persist_one_and_load(
            db, driver_id=driver_id, session_id=s1
        )
        _, opened_new = await upsert_weakness_episode(
            db, insight_row=ins_b, rule=_rule(),
            now=datetime.now(UTC),
        )
        ep = (
            await db.execute(select(WeaknessEpisode))
        ).scalar_one()
    assert opened_new is False
    assert ep.recurrence_count == 1


async def test_sessions_affected_does_not_duplicate_on_replay(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Same session_id appearing twice in sessions_affected would
    poison T209's recurrence-rate calculations and any analytics
    that count distinct sessions. Dedup at the source."""
    async with session_factory() as db:
        driver_id = await _seed_driver(db)
        s1 = await _add_session(db, driver_id)
        ins_a = await _persist_one_and_load(
            db, driver_id=driver_id, session_id=s1
        )
        await upsert_weakness_episode(
            db, insight_row=ins_a, rule=_rule(),
            now=datetime.now(UTC),
        )
        ins_b = await _persist_one_and_load(
            db, driver_id=driver_id, session_id=s1
        )
        await upsert_weakness_episode(
            db, insight_row=ins_b, rule=_rule(),
            now=datetime.now(UTC),
        )
        ep = (
            await db.execute(select(WeaknessEpisode))
        ).scalar_one()
    assert ep.sessions_affected == [str(s1)]


async def test_recurrence_counter_grows_to_n_across_n_sessions(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """End-to-end: 5 distinct sessions → counter=5. Pinning that
    growth is linear in session count, not insight count."""
    async with session_factory() as db:
        driver_id = await _seed_driver(db)
        for _ in range(5):
            s = await _add_session(db, driver_id)
            ins = await _persist_one_and_load(
                db, driver_id=driver_id, session_id=s
            )
            await upsert_weakness_episode(
                db, insight_row=ins, rule=_rule(),
                now=datetime.now(UTC),
            )
        ep = (
            await db.execute(select(WeaknessEpisode))
        ).scalar_one()
    assert ep.recurrence_count == 5
    assert len(ep.sessions_affected) == 5
