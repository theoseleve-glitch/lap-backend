"""T225 — Full loop: session→insight→episode→prescription→outcome.

End-to-end integration test for the coaching feedback loop.
The layer-by-layer tests (T207-T223) cover individual seams;
this one checks they wire together: the dataclass output of
`run_rules` round-trips into ORM rows, the episode dedup key
matches what the prescription generator expects, and the
outcome verdict propagates back to the originating episode
without losing the FK chain.

The walked path:

    1. Session 1 → CoachingInsight persisted (T207)
    2. Insight → WeaknessEpisode opened (T208)
    3. Episode → ActionPrescription generated, drill_template_id
       and success_condition wired through (T211-T213)
    4. Prescription dedup is idempotent (T214)
    5. Driver accepts the prescription (T215 simulated direct
       on the model)
    6. Session 2 outcome lands → ActionOutcome row created,
       prescription.status='completed', episode.confidence
       drops via T220 propagation

If this test breaks but the unit tests still pass, the seams
between layers have drifted — fix the integration, not the
units.
"""

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from coaching.library.rule_runner import CoachingInsight as RunnerInsight
from coaching.library.rule_schema import RuleSpec
from infra.coaching_insight_persistence import persist_insights
from infra.db import make_engine, make_session_factory
from infra.orm import (
    ActionOutcome,
    ActionPrescription,
    CoachingInsight,
    DriverProfile,
    Session,
    User,
    WeaknessEpisode,
)
from infra.outcome_persistence import persist_outcome
from infra.prescription_generator import generate_prescription_for_episode
from infra.weakness_episode_opener import upsert_weakness_episode

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def session_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[async_sessionmaker[AsyncSession], None, None]:
    db_path = tmp_path / "loop.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    yield factory


# Hand-rolled rule that mirrors what the YAML library would
# load — late-braking apex_speed_loss with a clean
# success_condition T219's evaluator can score against.
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
  beginner: "You lose km/h at the apex of T1."
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
    import yaml
    return RuleSpec.model_validate(yaml.safe_load(_RULE_YAML))


async def test_full_loop_session_insight_episode_prescription_outcome(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rule = _rule()
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    session1_id = uuid6.uuid7()
    session2_id = uuid6.uuid7()
    t0 = datetime.now(UTC)

    # ---- Setup: user, driver, two sessions ----
    async with session_factory() as db:
        db.add(
            User(
                id=user_id,
                clerk_user_id=f"u_loop_{user_id.hex}",
                email=f"loop_{user_id.hex}@test",
            )
        )
        db.add(
            DriverProfile(
                id=driver_id, user_id=user_id, display_name="A"
            )
        )
        db.add(
            Session(
                id=session1_id, driver_id=driver_id,
                manifest={}, status="ready", created_at=t0,
            )
        )
        db.add(
            Session(
                id=session2_id, driver_id=driver_id,
                manifest={}, status="ready", created_at=t0,
            )
        )
        await db.commit()

    # ---- Step 1: session 1 → insight persisted (T207) ----
    runner_insight = RunnerInsight(
        rule_id=rule.id,
        rule_version=rule.version,
        category=rule.category,
        subcategory=rule.subcategory,
        difficulty=rule.difficulty,
        explanation="apex deficit at T1",
    )
    async with session_factory() as db:
        insight_ids = await persist_insights(
            db, insights=[runner_insight],
            session_id=session1_id, driver_id=driver_id, now=t0,
        )
        insight_row = (
            await db.execute(
                select(CoachingInsight).where(
                    CoachingInsight.id == insight_ids[0]
                )
            )
        ).scalar_one()
    assert insight_row.rule_id == rule.id
    assert insight_row.driver_id == driver_id

    # ---- Step 2: insight → episode opened (T208) ----
    async with session_factory() as db:
        # Re-fetch insight in this session for upsert.
        ins = (
            await db.execute(
                select(CoachingInsight).where(
                    CoachingInsight.id == insight_ids[0]
                )
            )
        ).scalar_one()
        ep_id, opened_new = await upsert_weakness_episode(
            db, insight_row=ins, rule=rule, now=t0,
        )
        ep_row = (
            await db.execute(
                select(WeaknessEpisode).where(
                    WeaknessEpisode.id == ep_id
                )
            )
        ).scalar_one()
    assert opened_new is True
    assert ep_row.driver_id == driver_id
    assert ep_row.library_rule_id == rule.id
    assert str(session1_id) in ep_row.sessions_affected
    assert ep_row.confidence == pytest.approx(1.0)

    # ---- Step 3: episode → prescription (T211-T213) ----
    async with session_factory() as db:
        ep = (
            await db.execute(
                select(WeaknessEpisode).where(
                    WeaknessEpisode.id == ep_id
                )
            )
        ).scalar_one()
        pres_id = await generate_prescription_for_episode(
            db, episode=ep, rule=rule, now=t0,
        )
        pres_row = (
            await db.execute(
                select(ActionPrescription).where(
                    ActionPrescription.id == pres_id
                )
            )
        ).scalar_one()
    assert pres_row.weakness_episode_id == ep_id
    assert pres_row.drill_template_id == rule.prescription.drill_template_id
    assert pres_row.success_condition["metric"] == "apex_speed"
    assert pres_row.status == "proposed"

    # ---- Step 4: T214 dedup — second call is a no-op ----
    async with session_factory() as db:
        ep = (
            await db.execute(
                select(WeaknessEpisode).where(
                    WeaknessEpisode.id == ep_id
                )
            )
        ).scalar_one()
        dup_id = await generate_prescription_for_episode(
            db, episode=ep, rule=rule, now=t0,
        )
        prescriptions = (
            await db.execute(select(ActionPrescription))
        ).scalars().all()
    assert dup_id == pres_id
    assert len(prescriptions) == 1

    # ---- Step 5: driver accepts (simulating T215 endpoint) ----
    async with session_factory() as db:
        pres = (
            await db.execute(
                select(ActionPrescription).where(
                    ActionPrescription.id == pres_id
                )
            )
        ).scalar_one()
        pres.status = "accepted"
        pres.accepted_at = t0
        await db.commit()

    # ---- Step 6: session 2 outcome → completed (T219+T220) ----
    async with session_factory() as db:
        pres = (
            await db.execute(
                select(ActionPrescription).where(
                    ActionPrescription.id == pres_id
                )
            )
        ).scalar_one()
        outcome_id, verdict = await persist_outcome(
            db, prescription=pres,
            measured_value=96.0,  # at the 0.96 target
            reference_value=100.0,
            session_ids_measured=[str(session2_id)],
            measured_at=t0,
        )
        outcome_row = (
            await db.execute(
                select(ActionOutcome).where(
                    ActionOutcome.id == outcome_id
                )
            )
        ).scalar_one()
        pres_after = (
            await db.execute(
                select(ActionPrescription).where(
                    ActionPrescription.id == pres_id
                )
            )
        ).scalar_one()
        ep_after = (
            await db.execute(
                select(WeaknessEpisode).where(
                    WeaknessEpisode.id == ep_id
                )
            )
        ).scalar_one()

    # T218 evaluator says met (96/100 >= 0.96); T219 writes a row.
    assert verdict == "met"
    assert outcome_row.success_condition_met is True
    assert outcome_row.prescription_id == pres_id
    # T219 flips status; T220 propagates confidence (-0.5 from 1.0).
    assert pres_after.status == "completed"
    assert pres_after.outcome_id == outcome_id
    assert ep_after.confidence == pytest.approx(0.5)
