"""T219 — Outcome persistence wraps the T218 evaluator.

Given an open prescription + an observed measurement + a
reference baseline, this writes the verdict to the database:

    met / not_met → ActionOutcome row created;
                    prescription.status = 'completed';
                    prescription.outcome_id wired
    inconclusive  → no row, prescription untouched
                    (re-evaluate later when more data lands)

The unique constraint on `action_outcomes.prescription_id`
guarantees one outcome per prescription. Inconclusive intentionally
writes nothing — its absence IS the signal that the measurement
window hasn't yielded enough data yet.
"""

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import (
    ActionOutcome,
    ActionPrescription,
    DriverProfile,
    User,
    WeaknessEpisode,
)
from infra.outcome_persistence import persist_outcome

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def session_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[async_sessionmaker[AsyncSession], None, None]:
    db_path = tmp_path / "outcome.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    yield factory


_DEFAULT_CONDITION = {
    "metric": "apex_speed",
    "comparison": "gte",
    "target_pct": 0.96,
    "measurement_window": "next_3_sessions",
}


async def _seed_open_prescription(
    db: AsyncSession,
    *,
    success_condition: dict[str, object] | None = None,
    status: str = "accepted",
) -> ActionPrescription:
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    episode_id = uuid6.uuid7()
    prescription_id = uuid6.uuid7()
    now = datetime.now(UTC)
    db.add(
        User(
            id=user_id,
            clerk_user_id=f"u_oc_{user_id.hex}",
            email=f"oc_{user_id.hex}@test",
        )
    )
    db.add(
        DriverProfile(id=driver_id, user_id=user_id, display_name="A")
    )
    db.add(
        WeaknessEpisode(
            id=episode_id, driver_id=driver_id,
            category="technique", subcategory="late_braking",
            first_observed_at=now, last_observed_at=now,
            recurrence_count=1, sessions_affected=[],
            severity="medium", confidence=1.0, status="open",
            linked_prescriptions=[], linked_insights=[],
            library_rule_id="br.late_braking.apex_speed_loss",
        )
    )
    db.add(
        ActionPrescription(
            id=prescription_id, driver_id=driver_id,
            weakness_episode_id=episode_id, created_at=now,
            priority="p1", title="Address late braking",
            rationale="r", expected_context={},
            success_condition=success_condition or _DEFAULT_CONDITION,
            status=status, drill_template_id="drill.x",
        )
    )
    await db.commit()
    return (
        await db.execute(
            select(ActionPrescription).where(
                ActionPrescription.id == prescription_id
            )
        )
    ).scalar_one()


async def _read_outcome_for(
    db: AsyncSession, prescription_id: UUID
) -> ActionOutcome | None:
    return (
        await db.execute(
            select(ActionOutcome).where(
                ActionOutcome.prescription_id == prescription_id
            )
        )
    ).scalar_one_or_none()


async def test_met_creates_outcome_row_with_met_flag(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        pres = await _seed_open_prescription(db)
        outcome_id, verdict = await persist_outcome(
            db, prescription=pres,
            measured_value=96.0, reference_value=100.0,
            session_ids_measured=[str(uuid6.uuid7())],
            measured_at=datetime.now(UTC),
        )
        outcome = await _read_outcome_for(db, pres.id)
    assert verdict == "met"
    assert outcome_id is not None
    assert outcome is not None
    assert outcome.success_condition_met is True


async def test_met_flips_prescription_to_completed_with_outcome_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        pres = await _seed_open_prescription(db)
        outcome_id, _ = await persist_outcome(
            db, prescription=pres,
            measured_value=96.0, reference_value=100.0,
            session_ids_measured=[str(uuid6.uuid7())],
            measured_at=datetime.now(UTC),
        )
        refreshed = (
            await db.execute(
                select(ActionPrescription).where(
                    ActionPrescription.id == pres.id
                )
            )
        ).scalar_one()
    assert refreshed.status == "completed"
    assert refreshed.outcome_id == outcome_id


async def test_not_met_creates_outcome_row_with_failed_flag(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        pres = await _seed_open_prescription(db)
        outcome_id, verdict = await persist_outcome(
            db, prescription=pres,
            measured_value=95.0, reference_value=100.0,
            session_ids_measured=[],
            measured_at=datetime.now(UTC),
        )
        outcome = await _read_outcome_for(db, pres.id)
    assert verdict == "not_met"
    assert outcome_id is not None
    assert outcome is not None
    assert outcome.success_condition_met is False


async def test_not_met_still_completes_the_prescription(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A measured-and-failed prescription is terminal too — we
    measured, we have an answer, no point re-measuring."""
    async with session_factory() as db:
        pres = await _seed_open_prescription(db)
        await persist_outcome(
            db, prescription=pres,
            measured_value=95.0, reference_value=100.0,
            session_ids_measured=[],
            measured_at=datetime.now(UTC),
        )
        refreshed = (
            await db.execute(
                select(ActionPrescription).where(
                    ActionPrescription.id == pres.id
                )
            )
        ).scalar_one()
    assert refreshed.status == "completed"


async def test_inconclusive_writes_no_row_and_leaves_prescription_alone(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Reference of zero is undefined — no row, no status flip,
    so a later re-evaluation can write the real verdict."""
    async with session_factory() as db:
        pres = await _seed_open_prescription(db, status="accepted")
        outcome_id, verdict = await persist_outcome(
            db, prescription=pres,
            measured_value=10.0, reference_value=0.0,
            session_ids_measured=[],
            measured_at=datetime.now(UTC),
        )
        outcome = await _read_outcome_for(db, pres.id)
        refreshed = (
            await db.execute(
                select(ActionPrescription).where(
                    ActionPrescription.id == pres.id
                )
            )
        ).scalar_one()
    assert verdict == "inconclusive"
    assert outcome_id is None
    assert outcome is None
    assert refreshed.status == "accepted"
    assert refreshed.outcome_id is None


async def test_outcome_records_delta_vs_baseline(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """delta = observed - reference. Positive means improvement
    for 'higher is better' metrics; the sign carries the meaning,
    not just the magnitude."""
    async with session_factory() as db:
        pres = await _seed_open_prescription(db)
        await persist_outcome(
            db, prescription=pres,
            measured_value=98.5, reference_value=100.0,
            session_ids_measured=[],
            measured_at=datetime.now(UTC),
        )
        outcome = await _read_outcome_for(db, pres.id)
    assert outcome is not None
    assert outcome.delta_vs_baseline == pytest.approx(-1.5)


async def test_outcome_records_session_ids_measured(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The audit trail of which sessions contributed must round-trip."""
    sids = [str(uuid6.uuid7()), str(uuid6.uuid7())]
    async with session_factory() as db:
        pres = await _seed_open_prescription(db)
        await persist_outcome(
            db, prescription=pres,
            measured_value=96.0, reference_value=100.0,
            session_ids_measured=sids,
            measured_at=datetime.now(UTC),
        )
        outcome = await _read_outcome_for(db, pres.id)
    assert outcome is not None
    assert outcome.session_ids_measured == sids
