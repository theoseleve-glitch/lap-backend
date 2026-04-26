"""T065 (sprint2-backend) — cohort unlock notifications.

When a (master_car, master_track) cohort crosses k=20 (T056), a
trigger populates DriverBenchmarkProfile rows for every opt-in
driver who has contributed. This is the "event" the spec calls
out — in this codebase the event is just the function call;
T063's idempotent upsert means re-firing on an already-unlocked
cohort is harmless and keeps profiles current.

The trigger composes:
  - is_cohort_eligible_for_percentile (T056) — gate
  - refresh_driver_benchmark_profile (T063) — per-driver work

Acceptance:
  - locked cohort → no profiles created, returns []
  - unlocked cohort, no existing profiles → all opt-in drivers
    get a profile, returns their ids
  - re-fire on unlocked cohort with profiles → no duplicates
    (idempotent), returns same set
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select

from alembic import command
from domain.cohort_unlock import process_cohort_unlock
from infra.db import make_engine, make_session_factory
from infra.orm import (
    CarModel,
    CohortMetric,
    DriverBenchmarkProfile,
    DriverProfile,
    DriverProgressSnapshot,
    TrackLayout,
    User,
)
from infra.orm import Session as SessionRow

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def upgraded_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


def _dim(score: float) -> dict[str, Any]:
    return {
        "score": score, "confidence": 0.8, "trend_14d": "flat",
        "last_evidence_session_id": None, "sample_count": 5,
    }


async def _seed_cohort(
    factory: Any, *, n_drivers: int, label: str = "a",
) -> dict[str, Any]:
    """Seed a (track, car) plus n_drivers opt-in drivers each
    with one session, a snapshot, and a cohort_metric row.

    `label` distinguishes provider/name strings + clerk ids so
    multiple cohorts can co-exist in one test."""
    master_track = uuid6.uuid7()
    master_car = uuid6.uuid7()
    async with factory() as db:
        track = TrackLayout(
            id=uuid6.uuid7(), source_provider=f"iracing-{label}",
            name=f"Spa-{label}", layout_name="GP",
            start_finish_lat=50.4, start_finish_lon=5.97,
            start_finish_heading_deg=10.0,
            sector_checkpoints=[], corner_names=[],
            master_track_id=master_track,
        )
        car = CarModel(
            id=uuid6.uuid7(), source_provider=f"iracing-{label}",
            name=f"911-{label}", car_class="gt3",
            master_car_id=master_car,
        )
        db.add_all([track, car])
        await db.commit()
        driver_ids = []
        for i in range(n_drivers):
            user = User(
                id=uuid6.uuid7(), clerk_user_id=f"{label}d{i}",
                email=f"{label}d{i}@t",
            )
            driver = DriverProfile(
                id=uuid6.uuid7(), user_id=user.id,
                display_name=f"D{i}", cohort_opt_in=True,
            )
            session = SessionRow(
                id=uuid6.uuid7(), driver_id=driver.id,
                manifest={
                    "track_id": str(track.id),
                    "car_id": str(car.id),
                },
                created_at=datetime(2026, 4, 26, tzinfo=UTC),
                status="processed",
            )
            snap = DriverProgressSnapshot(
                id=uuid6.uuid7(), driver_id=driver.id,
                captured_at=datetime(2026, 4, 26, tzinfo=UTC),
                window="session",
                window_start=datetime(2026, 4, 26, tzinfo=UTC),
                window_end=datetime(2026, 4, 26, tzinfo=UTC),
                braking=_dim(50.0 + i),
                throttle_pickup=_dim(60.0),
                minimum_speed=_dim(60.0),
                line_consistency=_dim(60.0),
                tire_management=_dim(60.0),
                racecraft=_dim(60.0),
                fatigue_consistency=_dim(60.0),
                strategy_execution=_dim(60.0),
                setup_sensitivity=_dim(60.0),
                adaptation=_dim(60.0),
                overall_score=60.0, overall_confidence=0.7,
                generated_by="t065-test",
            )
            db.add_all([user, driver, session, snap])
            db.add(
                CohortMetric(
                    id=uuid6.uuid7(),
                    car_id=master_car, track_id=master_track,
                    metric="braking", value=50.0 + i,
                    session_time=datetime(2026, 4, 26, tzinfo=UTC),
                )
            )
            driver_ids.append(driver.id)
        await db.commit()
        return {
            "master_track": master_track, "master_car": master_car,
            "driver_ids": driver_ids,
        }


# -- locked cohort ------------------------------------------------


def test_locked_cohort_creates_no_profiles(
    upgraded_db_url: str,
) -> None:
    """5 opt-in drivers (well below k=20) → no profiles, []
    returned. Pin: the trigger respects the threshold gate."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            seed = await _seed_cohort(factory, n_drivers=5)
            async with factory() as db:
                refreshed = await process_cohort_unlock(
                    db,
                    car_id=seed["master_car"],
                    track_id=seed["master_track"],
                )
                await db.commit()
            assert refreshed == []
            async with factory() as db:
                rows = (
                    (await db.execute(select(DriverBenchmarkProfile)))
                    .scalars().all()
                )
                assert rows == []
        finally:
            await engine.dispose()

    asyncio.run(run())


# -- unlocked cohort ----------------------------------------------


def test_unlocked_cohort_creates_profiles_for_all_opt_in_drivers(
    upgraded_db_url: str,
) -> None:
    """20 opt-in drivers (k threshold met) → 20 profiles created,
    one per driver. Returned ids match the cohort members."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            seed = await _seed_cohort(factory, n_drivers=20)
            async with factory() as db:
                refreshed = await process_cohort_unlock(
                    db,
                    car_id=seed["master_car"],
                    track_id=seed["master_track"],
                )
                await db.commit()
            assert len(refreshed) == 20
            assert set(refreshed) == set(seed["driver_ids"])
            async with factory() as db:
                rows = (
                    (await db.execute(select(DriverBenchmarkProfile)))
                    .scalars().all()
                )
                assert len(rows) == 20
        finally:
            await engine.dispose()

    asyncio.run(run())


# -- idempotency --------------------------------------------------


def test_unlock_does_not_refresh_drivers_from_other_cohorts(
    upgraded_db_url: str,
) -> None:
    """Two cohorts coexist: A (20 drivers, target of unlock) and
    B (20 drivers, different car/track). Calling unlock for A
    refreshes ONLY A's drivers — B's stay without profiles.
    Pin the cohort filter (master_car / master_track WHERE clause).
    Mutation: dropping the filter would refresh both cohorts'
    drivers, growing rows from 20 to 40."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            seed_a = await _seed_cohort(
                factory, n_drivers=20, label="a",
            )
            seed_b = await _seed_cohort(
                factory, n_drivers=20, label="b",
            )
            async with factory() as db:
                refreshed = await process_cohort_unlock(
                    db,
                    car_id=seed_a["master_car"],
                    track_id=seed_a["master_track"],
                )
                await db.commit()
            assert len(refreshed) == 20
            assert set(refreshed) == set(seed_a["driver_ids"])
            assert not (set(refreshed) & set(seed_b["driver_ids"]))
            async with factory() as db:
                rows = (
                    (await db.execute(select(DriverBenchmarkProfile)))
                    .scalars().all()
                )
                # Only cohort A produced profiles.
                assert len(rows) == 20
                profile_drivers = {r.driver_id for r in rows}
                assert profile_drivers == set(seed_a["driver_ids"])
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_re_fire_on_unlocked_cohort_does_not_duplicate(
    upgraded_db_url: str,
) -> None:
    """Calling process_cohort_unlock TWICE on the same unlocked
    cohort produces 20 rows total, not 40. Inherits T063's
    upsert idempotency. Critical because the trigger may fire
    on every ingest after the unlock — duplication would
    explode the table."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            seed = await _seed_cohort(factory, n_drivers=20)
            async with factory() as db:
                first = await process_cohort_unlock(
                    db,
                    car_id=seed["master_car"],
                    track_id=seed["master_track"],
                )
                await db.commit()
            async with factory() as db:
                second = await process_cohort_unlock(
                    db,
                    car_id=seed["master_car"],
                    track_id=seed["master_track"],
                )
                await db.commit()
            assert len(first) == 20
            assert len(second) == 20
            async with factory() as db:
                rows = (
                    (await db.execute(select(DriverBenchmarkProfile)))
                    .scalars().all()
                )
                assert len(rows) == 20
        finally:
            await engine.dispose()

    asyncio.run(run())
