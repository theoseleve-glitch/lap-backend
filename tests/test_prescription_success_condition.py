"""T213 — Prescription `success_condition` populated from rule.

T211 left `success_condition` as an empty dict. T213 fills it
from `rule.prescription.success_condition` so the outcome
tracker (T-future) can score whether the driver met the bar
without a human deciding what 'better' looks like.

The shape is fixed by the SuccessCondition pydantic model —
metric / comparison / target_pct / measurement_window. Pinning
all four so a future schema rename can't silently drop a field
(empty dict would still be a dict).
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
    db_path = tmp_path / "succ.sqlite"
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
            clerk_user_id=f"u_succ_{user_id.hex}",
            email=f"succ_{user_id.hex}@test",
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


async def test_prescription_success_condition_carries_all_four_fields(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """All four SuccessCondition fields ride into the row so the
    outcome tracker has a complete machine-checkable contract."""
    async with session_factory() as db:
        episode = await _seed_open_episode(db)
        await generate_prescription_for_episode(
            db, episode=episode, rule=_rule(), now=datetime.now(UTC)
        )
        row = (
            await db.execute(select(ActionPrescription))
        ).scalar_one()
    assert row.success_condition == {
        "metric": "apex_speed",
        "comparison": "gte",
        "target_pct": 0.96,
        "measurement_window": "next_3_sessions",
    }
