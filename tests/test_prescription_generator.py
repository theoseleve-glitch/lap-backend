"""T211 — Prescription generator skeleton.

When a WeaknessEpisode opens (or escalates), the system writes
an ActionPrescription row linked to the episode. T211 nails the
basic creation path — id, driver_id, weakness_episode_id,
status='proposed', title/rationale, default priority. Layer-by-
layer:

    T212 — drill_template_id from the rule
    T213 — success_condition populated from the rule template
    T214 — dedup: same open episode → no second prescription

Each layer adds its assertion to the contract; this file pins
the foundational invariants T212-T214 build on.
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
from coaching.library.rule_schema import RuleSpec
from infra.db import make_engine, make_session_factory
from infra.orm import (
    ActionPrescription,
    DriverProfile,
    User,
    WeaknessEpisode,
)
from infra.prescription_generator import generate_prescription_for_episode

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def session_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[async_sessionmaker[AsyncSession], None, None]:
    db_path = tmp_path / "presc.sqlite"
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
severity_rule: "linear, 0.93=low, 0.88=medium, 0.82=high"
explanation_template:
  beginner: "You lose {{delta_kmh}} km/h at the apex of {{corner}}."
  intermediate: "i"
  advanced: "a"
prescription:
  drill_template_id: "drill.trail_brake_later_release"
  expected_gain_kmh: "2-5"
  success_condition:
    metric: "apex_speed"
    comparison: "gte"
    target_pct: 0.96
    measurement_window: "next_3_sessions"
"""


def _rule() -> RuleSpec:
    return RuleSpec.model_validate(yaml.safe_load(_RULE_YAML))


async def _seed_open_episode(
    db: AsyncSession,
) -> tuple[UUID, WeaknessEpisode]:
    """Create a User+Driver+WeaknessEpisode in 'open' status.
    Returns (driver_id, episode_row)."""
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    episode_id = uuid6.uuid7()
    now = datetime.now(UTC)
    db.add(
        User(
            id=user_id,
            clerk_user_id=f"u_pres_{user_id.hex}",
            email=f"pres_{user_id.hex}@test",
        )
    )
    db.add(
        DriverProfile(id=driver_id, user_id=user_id, display_name="A")
    )
    db.add(
        WeaknessEpisode(
            id=episode_id,
            driver_id=driver_id,
            category="technique",
            subcategory="late_braking",
            first_observed_at=now,
            last_observed_at=now,
            recurrence_count=1,
            sessions_affected=[],
            severity="medium",
            confidence=1.0,
            status="open",
            linked_prescriptions=[],
            linked_insights=[],
            library_rule_id="br.late_braking.apex_speed_loss",
        )
    )
    await db.commit()
    episode = (
        await db.execute(
            select(WeaknessEpisode).where(
                WeaknessEpisode.id == episode_id
            )
        )
    ).scalar_one()
    return driver_id, episode


async def test_generate_creates_one_prescription_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        _driver_id, episode = await _seed_open_episode(db)
        prescription_id = await generate_prescription_for_episode(
            db, episode=episode, rule=_rule(), now=datetime.now(UTC)
        )
        rows = (
            await db.execute(select(ActionPrescription))
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == prescription_id


async def test_prescription_links_to_episode_and_driver(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """weakness_episode_id and driver_id come from the episode —
    not arguments to the function. The episode IS the source of
    truth for who's getting the prescription."""
    async with session_factory() as db:
        driver_id, episode = await _seed_open_episode(db)
        await generate_prescription_for_episode(
            db, episode=episode, rule=_rule(), now=datetime.now(UTC)
        )
        row = (
            await db.execute(select(ActionPrescription))
        ).scalar_one()
    assert row.driver_id == driver_id
    assert row.weakness_episode_id == episode.id


async def test_prescription_status_is_proposed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A freshly generated prescription is awaiting driver
    review — `proposed`. T215/T216 will flip it to
    accepted/skipped via the user-facing endpoints."""
    async with session_factory() as db:
        _driver_id, episode = await _seed_open_episode(db)
        await generate_prescription_for_episode(
            db, episode=episode, rule=_rule(), now=datetime.now(UTC)
        )
        row = (
            await db.execute(select(ActionPrescription))
        ).scalar_one()
    assert row.status == "proposed"
    assert row.accepted_at is None


async def test_prescription_default_priority_is_p1(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ADR-005 priority enum is p0/p1/p2. p0 is in-session
    urgency; p2 is whenever. New episodes are p1 — 'before next
    session' — until escalation logic upgrades them."""
    async with session_factory() as db:
        _driver_id, episode = await _seed_open_episode(db)
        await generate_prescription_for_episode(
            db, episode=episode, rule=_rule(), now=datetime.now(UTC)
        )
        row = (
            await db.execute(select(ActionPrescription))
        ).scalar_one()
    assert row.priority == "p1"


async def test_prescription_carries_non_empty_title_and_rationale(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Both fields are required on the ORM. Title is the
    one-line UI label; rationale is the plain-language why.
    Pinning non-emptiness — the actual phrasing can evolve, but
    a blank string would render as a broken UI cell."""
    async with session_factory() as db:
        _driver_id, episode = await _seed_open_episode(db)
        await generate_prescription_for_episode(
            db, episode=episode, rule=_rule(), now=datetime.now(UTC)
        )
        row = (
            await db.execute(select(ActionPrescription))
        ).scalar_one()
    assert row.title.strip()
    assert row.rationale.strip()


async def test_prescription_uses_supplied_now_for_created_at(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Caller-supplied now lets a Celery task replay deterministically."""
    fixed = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    async with session_factory() as db:
        _driver_id, episode = await _seed_open_episode(db)
        await generate_prescription_for_episode(
            db, episode=episode, rule=_rule(), now=fixed
        )
        row = (
            await db.execute(select(ActionPrescription))
        ).scalar_one()
    assert row.created_at.replace(tzinfo=UTC) == fixed


async def test_prescription_initializes_required_json_columns(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """expected_context and success_condition are NOT NULL
    JSON columns. T211 ships them as empty dicts; T213 fills
    success_condition from the rule template, T-future fills
    expected_context once we know what session the prescription
    is for. Empty dict beats null both for SQL and for the
    Pydantic model that wraps the column."""
    async with session_factory() as db:
        _driver_id, episode = await _seed_open_episode(db)
        await generate_prescription_for_episode(
            db, episode=episode, rule=_rule(), now=datetime.now(UTC)
        )
        row = (
            await db.execute(select(ActionPrescription))
        ).scalar_one()
    assert row.expected_context == {}
    # success_condition is allowed to be empty here — T213 will
    # populate it. The column just must not be null.
    assert isinstance(row.success_condition, dict)


async def test_prescription_id_is_uuid7(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Per CLAUDE.md, all IDs are UUID7 (time-sortable). Two
    prescriptions generated in order should sort accordingly."""
    async with session_factory() as db:
        _driver_id, episode_a = await _seed_open_episode(db)
        _driver_id_b, episode_b = await _seed_open_episode(db)
        id_a = await generate_prescription_for_episode(
            db, episode=episode_a, rule=_rule(),
            now=datetime.now(UTC),
        )
        id_b = await generate_prescription_for_episode(
            db, episode=episode_b, rule=_rule(),
            now=datetime.now(UTC),
        )
    assert id_a < id_b
