from datetime import UTC, datetime
from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import (
    ActionOutcome,
    ActionPrescription,
    DriverProfile,
    User,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

_CTX = {
    "car_id": str(uuid6.uuid7()),
    "track_id": str(uuid6.uuid7()),
    "track_layout": "gp",
}
_COND = {
    "metric": "sector_2_time_ms",
    "target_value": 27500.0,
    "comparison": "lte",
    "measurement_window": "next_3_sessions",
}


@pytest.fixture
def upgraded_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


async def _seed_prescription(db) -> ActionPrescription:  # type: ignore[no-untyped-def]
    user = User(id=uuid6.uuid7(), clerk_user_id="u", email="u@test")
    driver = DriverProfile(id=uuid6.uuid7(), user_id=user.id, display_name="A")
    db.add_all([user, driver])
    await db.flush()
    pres = ActionPrescription(
        id=uuid6.uuid7(),
        driver_id=driver.id,
        created_at=datetime(2026, 4, 25, tzinfo=UTC),
        priority="p1",
        title="t",
        rationale="r",
        expected_context=_CTX,
        success_condition=_COND,
        status="accepted",
    )
    db.add(pres)
    await db.commit()
    return pres


async def test_action_outcome_roundtrips_all_fields(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            pres = await _seed_prescription(db)
            measured = datetime(2026, 4, 27, 14, 30, tzinfo=UTC)
            sids = [str(uuid6.uuid7()) for _ in range(3)]
            outcome = ActionOutcome(
                id=uuid6.uuid7(),
                prescription_id=pres.id,
                measured_at=measured,
                session_ids_measured=sids,
                success_condition_met=True,
                measured_value=27_200.5,
                delta_vs_baseline=-450.3,
                confidence_change=0.15,
                prescription_efficacy_score=0.82,
                notes="Driver applied progressive trail-brake release as prescribed.",
            )
            db.add(outcome)
            await db.commit()

            loaded = (await db.execute(select(ActionOutcome))).scalar_one()
            assert loaded.prescription_id == pres.id
            assert loaded.measured_at == measured
            assert loaded.session_ids_measured == sids
            assert loaded.success_condition_met is True
            assert loaded.measured_value == 27_200.5
            assert loaded.delta_vs_baseline == -450.3
            assert loaded.confidence_change == 0.15
            assert loaded.prescription_efficacy_score == 0.82
            assert loaded.notes is not None
            assert "progressive" in loaded.notes
    finally:
        await engine.dispose()


async def test_action_outcome_defaults_and_null_notes(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            pres = await _seed_prescription(db)
            outcome = ActionOutcome(
                id=uuid6.uuid7(),
                prescription_id=pres.id,
                measured_at=datetime(2026, 4, 27, tzinfo=UTC),
                success_condition_met=False,
                measured_value=28_000.0,
                delta_vs_baseline=50.0,
                confidence_change=-0.05,
                prescription_efficacy_score=0.3,
            )
            db.add(outcome)
            await db.commit()
            loaded = (await db.execute(select(ActionOutcome))).scalar_one()
            assert loaded.session_ids_measured == []
            assert loaded.notes is None
    finally:
        await engine.dispose()


async def test_action_outcome_prescription_id_is_unique(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            pres = await _seed_prescription(db)
            db.add(
                ActionOutcome(
                    id=uuid6.uuid7(),
                    prescription_id=pres.id,
                    measured_at=datetime(2026, 4, 27, tzinfo=UTC),
                    success_condition_met=True,
                    measured_value=1.0,
                    delta_vs_baseline=0.0,
                    confidence_change=0.0,
                    prescription_efficacy_score=0.5,
                )
            )
            await db.commit()
        async with session_factory() as db:
            db.add(
                ActionOutcome(
                    id=uuid6.uuid7(),
                    prescription_id=pres.id,
                    measured_at=datetime(2026, 4, 28, tzinfo=UTC),
                    success_condition_met=False,
                    measured_value=2.0,
                    delta_vs_baseline=1.0,
                    confidence_change=-0.1,
                    prescription_efficacy_score=0.4,
                )
            )
            with pytest.raises(IntegrityError):
                await db.commit()
    finally:
        await engine.dispose()
