from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, DriverProgressSnapshot, User

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def _dim(score: float, trend: str = "flat") -> dict[str, Any]:
    return {
        "score": score,
        "confidence": 0.8,
        "trend_14d": trend,
        "last_evidence_session_id": None,
        "sample_count": 20,
    }


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


async def test_driver_progress_snapshot_roundtrips_all_fields(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            captured = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
            start = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)
            end = captured

            snap = DriverProgressSnapshot(
                id=uuid6.uuid7(),
                driver_id=driver.id,
                captured_at=captured,
                window="week",
                window_start=start,
                window_end=end,
                braking=_dim(72.5, "up"),
                throttle_pickup=_dim(68.0),
                minimum_speed=_dim(65.0, "down"),
                line_consistency=_dim(70.0),
                tire_management=_dim(60.0, "volatile"),
                racecraft=_dim(55.0),
                fatigue_consistency=_dim(80.0, "up"),
                strategy_execution=_dim(50.0),
                setup_sensitivity=_dim(45.0),
                adaptation=_dim(62.0, "up"),
                overall_score=62.75,
                overall_confidence=0.78,
                percentile_vs_cohort=0.62,
                generated_by="coaching_engine.v3",
            )
            db.add(snap)
            await db.commit()

            loaded = (
                await db.execute(select(DriverProgressSnapshot))
            ).scalar_one()
            assert loaded.driver_id == driver.id
            assert loaded.captured_at == captured
            assert loaded.window == "week"
            assert loaded.window_start == start
            assert loaded.window_end == end
            assert loaded.braking["score"] == 72.5
            assert loaded.braking["trend_14d"] == "up"
            assert loaded.throttle_pickup["score"] == 68.0
            assert loaded.minimum_speed["trend_14d"] == "down"
            assert loaded.line_consistency["sample_count"] == 20
            assert loaded.tire_management["trend_14d"] == "volatile"
            assert loaded.racecraft["score"] == 55.0
            assert loaded.fatigue_consistency["score"] == 80.0
            assert loaded.strategy_execution["score"] == 50.0
            assert loaded.setup_sensitivity["score"] == 45.0
            assert loaded.adaptation["trend_14d"] == "up"
            assert loaded.overall_score == 62.75
            assert loaded.overall_confidence == 0.78
            assert loaded.percentile_vs_cohort == 0.62
            assert loaded.generated_by == "coaching_engine.v3"
    finally:
        await engine.dispose()


async def test_percentile_vs_cohort_is_nullable(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            now = datetime(2026, 4, 25, tzinfo=UTC)
            snap = DriverProgressSnapshot(
                id=uuid6.uuid7(),
                driver_id=driver.id,
                captured_at=now,
                window="session",
                window_start=now,
                window_end=now,
                braking=_dim(50.0),
                throttle_pickup=_dim(50.0),
                minimum_speed=_dim(50.0),
                line_consistency=_dim(50.0),
                tire_management=_dim(50.0),
                racecraft=_dim(50.0),
                fatigue_consistency=_dim(50.0),
                strategy_execution=_dim(50.0),
                setup_sensitivity=_dim(50.0),
                adaptation=_dim(50.0),
                overall_score=50.0,
                overall_confidence=0.5,
                percentile_vs_cohort=None,
                generated_by="coaching_engine.v3",
            )
            db.add(snap)
            await db.commit()
            loaded = (
                await db.execute(select(DriverProgressSnapshot))
            ).scalar_one()
            assert loaded.percentile_vs_cohort is None
    finally:
        await engine.dispose()
