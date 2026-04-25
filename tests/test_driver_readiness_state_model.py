from datetime import UTC, datetime
from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, DriverReadinessState, User

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def upgraded_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


async def _seed_driver(db) -> DriverProfile:  # type: ignore[no-untyped-def]
    user = User(id=uuid6.uuid7(), clerk_user_id="u", email="u@test")
    driver = DriverProfile(id=uuid6.uuid7(), user_id=user.id, display_name="A")
    db.add_all([user, driver])
    await db.commit()
    return driver


async def test_driver_readiness_state_roundtrips_all_fields(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            computed = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
            target = {
                "id": str(uuid6.uuid7()),
                "name": "Spa 6h Endurance",
                "kind": "sim_event",
                "starts_at": "2026-05-15T10:00:00+00:00",
                "ends_at": "2026-05-15T16:00:00+00:00",
                "track_id": str(uuid6.uuid7()),
                "car_id": str(uuid6.uuid7()),
                "competitive_weight": 0.85,
                "notes": "Championship round",
            }
            state = DriverReadinessState(
                id=uuid6.uuid7(),
                driver_id=driver.id,
                target_event=target,
                computed_at=computed,
                readiness_score=73.5,
                volume_input=0.8,
                quality_input=0.7,
                recency_input=0.9,
                consistency_input=0.65,
                unresolved_weaknesses_input=0.4,
                familiarity_input=0.85,
                event_pressure_input=0.6,
                recovery_input=0.9,
                limiting_factor="unresolved_weaknesses",
                likely_gain_area="trail_braking_T3",
                recommended_sessions_before_event=4,
            )
            db.add(state)
            await db.commit()

            loaded = (
                await db.execute(select(DriverReadinessState))
            ).scalar_one()
            assert loaded.driver_id == driver.id
            assert loaded.target_event == target
            assert loaded.computed_at == computed
            assert loaded.readiness_score == 73.5
            assert loaded.volume_input == 0.8
            assert loaded.quality_input == 0.7
            assert loaded.recency_input == 0.9
            assert loaded.consistency_input == 0.65
            assert loaded.unresolved_weaknesses_input == 0.4
            assert loaded.familiarity_input == 0.85
            assert loaded.event_pressure_input == 0.6
            assert loaded.recovery_input == 0.9
            assert loaded.limiting_factor == "unresolved_weaknesses"
            assert loaded.likely_gain_area == "trail_braking_T3"
            assert loaded.recommended_sessions_before_event == 4
    finally:
        await engine.dispose()


async def test_driver_readiness_state_allows_null_target_event(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            state = DriverReadinessState(
                id=uuid6.uuid7(),
                driver_id=driver.id,
                target_event=None,
                computed_at=datetime(2026, 4, 25, tzinfo=UTC),
                readiness_score=50.0,
                volume_input=0.5,
                quality_input=0.5,
                recency_input=0.5,
                consistency_input=0.5,
                unresolved_weaknesses_input=0.5,
                familiarity_input=0.5,
                event_pressure_input=0.0,
                recovery_input=0.5,
                limiting_factor="recency",
                likely_gain_area="general",
                recommended_sessions_before_event=2,
            )
            db.add(state)
            await db.commit()
            loaded = (
                await db.execute(select(DriverReadinessState))
            ).scalar_one()
            assert loaded.target_event is None
    finally:
        await engine.dispose()
