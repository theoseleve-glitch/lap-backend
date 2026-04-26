"""T214 — Prescription dedup against open WeaknessEpisode.

The Celery task that wires episode → prescription must be safe
to retry: a transient failure mid-pipeline can re-fire the
generator with the same episode. T214 pins the contract that
calling `generate_prescription_for_episode` twice for the same
episode leaves one row, and both calls return the same id —
the second call is a no-op finder, not an insert.

T211 created prescriptions unconditionally; T214 wraps the
insert in a "do we already have one?" guard. The episode IS
the dedup key — its (driver_id, library_rule_id, corner_tag)
uniqueness is already enforced upstream by T208's open-episode
upsert, so a single weakness can never spawn two open episodes,
which means a single weakness can never spawn two prescriptions
either.
"""

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

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
    db_path = tmp_path / "dedup.sqlite"
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


async def _seed_open_episode(db: AsyncSession) -> WeaknessEpisode:
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    episode_id = uuid6.uuid7()
    now = datetime.now(UTC)
    db.add(
        User(
            id=user_id,
            clerk_user_id=f"u_dedup_{user_id.hex}",
            email=f"dedup_{user_id.hex}@test",
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
    return (
        await db.execute(
            select(WeaknessEpisode).where(
                WeaknessEpisode.id == episode_id
            )
        )
    ).scalar_one()


async def test_second_generate_call_returns_existing_prescription(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two calls, one row. Second call returns the first id."""
    async with session_factory() as db:
        episode = await _seed_open_episode(db)
        first_id = await generate_prescription_for_episode(
            db, episode=episode, rule=_rule(), now=datetime.now(UTC)
        )
        second_id = await generate_prescription_for_episode(
            db, episode=episode, rule=_rule(), now=datetime.now(UTC)
        )
        rows = (
            await db.execute(select(ActionPrescription))
        ).scalars().all()
    assert len(rows) == 1
    assert first_id == second_id
