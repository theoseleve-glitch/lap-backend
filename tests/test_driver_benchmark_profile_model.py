from datetime import UTC, datetime
from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import DriverBenchmarkProfile, DriverProfile, User

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


async def test_driver_benchmark_profile_roundtrips_all_fields(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            generated = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
            cohort_def = {
                "skill_level": "intermediate",
                "car_class": "gt3",
                "track_id": str(uuid6.uuid7()),
                "environment_tag": "sim_practice",
                "min_sessions": 10,
            }
            per_track_car = {"spa:gt3.911": 0.72, "monza:gt3.720s": 0.81}
            per_dim = {"braking": 0.65, "line_consistency": 0.77}

            profile = DriverBenchmarkProfile(
                id=uuid6.uuid7(),
                driver_id=driver.id,
                generated_at=generated,
                cohort_definition=cohort_def,
                cohort_size=142,
                per_track_car_percentiles=per_track_car,
                per_dimension_percentiles=per_dim,
                archetype="late_braker",
            )
            db.add(profile)
            await db.commit()

            loaded = (
                await db.execute(select(DriverBenchmarkProfile))
            ).scalar_one()
            assert loaded.driver_id == driver.id
            assert loaded.generated_at == generated
            assert loaded.cohort_definition == cohort_def
            assert loaded.cohort_size == 142
            assert loaded.per_track_car_percentiles == per_track_car
            assert loaded.per_dimension_percentiles == per_dim
            assert loaded.archetype == "late_braker"
    finally:
        await engine.dispose()


async def test_driver_benchmark_profile_defaults_empty_dicts_and_null_archetype(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            profile = DriverBenchmarkProfile(
                id=uuid6.uuid7(),
                driver_id=driver.id,
                generated_at=datetime(2026, 4, 25, tzinfo=UTC),
                cohort_definition={"min_sessions": 5},
                cohort_size=0,
            )
            db.add(profile)
            await db.commit()
            loaded = (
                await db.execute(select(DriverBenchmarkProfile))
            ).scalar_one()
            assert loaded.per_track_car_percentiles == {}
            assert loaded.per_dimension_percentiles == {}
            assert loaded.archetype is None
    finally:
        await engine.dispose()
