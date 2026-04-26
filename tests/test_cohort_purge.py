"""T062 (sprint2-backend) — cohort data deletion on opt-out.

When a driver flips cohort_opt_in False (T053), their previously
ingested cohort_metric rows must be purged. cohort_metrics has
no driver_id (T054 invariant), so the deletion re-derives the
attribution: for each of the driver's sessions, find rows
matching (master_car_id, master_track_id, session_time) — those
are the rows T055 wrote for that session.

The (car, track, session_time) tuple is the natural key linking
ingestion to deletion since session.created_at is microsecond-
precise and unique per session. Two drivers on the same (car,
track) have different session_times, so the WHERE clause can't
accidentally delete another driver's rows.

Acceptance:
  - purge returns the number of deleted rows
  - all of the driver's metric rows are removed
  - other drivers' rows on the same (car, track) are unaffected
  - driver with no sessions → 0 deleted, no error
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select

from alembic import command
from domain.cohort_purge import purge_cohort_metrics_for_driver
from infra.db import make_engine, make_session_factory
from infra.orm import (
    CarModel,
    CohortMetric,
    DriverProfile,
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


_DIMENSIONS = (
    "braking", "throttle_pickup", "minimum_speed", "line_consistency",
    "tire_management", "racecraft", "fatigue_consistency",
    "strategy_execution", "setup_sensitivity", "adaptation",
)


async def _seed_track_car(
    factory: Any, *, label: str = "a",
) -> dict[str, Any]:
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
        return {
            "master_track": master_track, "master_car": master_car,
            "track_id": track.id, "car_id": car.id,
        }


async def _seed_driver_with_session_and_metrics(
    factory: Any, *, clerk_id: str, opt_in: bool,
    track_id: Any, car_id: Any,
    master_track: Any, master_car: Any,
    session_time: datetime,
) -> dict[str, Any]:
    """Seed a driver, one session, and 10 cohort_metric rows
    matching that session — mimics what T055 ingestion produces."""
    async with factory() as db:
        user = User(
            id=uuid6.uuid7(), clerk_user_id=clerk_id,
            email=f"{clerk_id}@t",
        )
        driver = DriverProfile(
            id=uuid6.uuid7(), user_id=user.id, display_name=clerk_id,
            cohort_opt_in=opt_in,
        )
        session = SessionRow(
            id=uuid6.uuid7(), driver_id=driver.id,
            manifest={"track_id": str(track_id), "car_id": str(car_id)},
            created_at=session_time, status="processed",
        )
        db.add_all([user, driver, session])
        for dim in _DIMENSIONS:
            db.add(
                CohortMetric(
                    id=uuid6.uuid7(),
                    car_id=master_car, track_id=master_track,
                    metric=dim, value=50.0,
                    session_time=session_time,
                )
            )
        await db.commit()
        return {"driver_id": driver.id, "session_id": session.id}


# -- happy paths --------------------------------------------------


def test_purge_removes_all_rows_for_single_session(
    upgraded_db_url: str,
) -> None:
    """One driver, one session → 10 cohort_metric rows. Purge
    deletes all 10 and returns 10."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            ids = await _seed_track_car(factory)
            seed = await _seed_driver_with_session_and_metrics(
                factory, clerk_id="alice", opt_in=False,
                track_id=ids["track_id"], car_id=ids["car_id"],
                master_track=ids["master_track"],
                master_car=ids["master_car"],
                session_time=datetime(2026, 4, 26, 14, tzinfo=UTC),
            )
            async with factory() as db:
                deleted = await purge_cohort_metrics_for_driver(
                    db, driver_id=seed["driver_id"]
                )
                await db.commit()
            assert deleted == 10
            async with factory() as db:
                remaining = (
                    (await db.execute(select(CohortMetric)))
                    .scalars().all()
                )
                assert remaining == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_purge_removes_rows_across_multiple_sessions(
    upgraded_db_url: str,
) -> None:
    """One driver with 3 sessions on the same (car, track) at
    different times → 30 rows total → all 30 deleted."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            ids = await _seed_track_car(factory)
            base = datetime(2026, 4, 26, tzinfo=UTC)
            # Seed alice with first session via the helper.
            seed = await _seed_driver_with_session_and_metrics(
                factory, clerk_id="alice", opt_in=False,
                track_id=ids["track_id"], car_id=ids["car_id"],
                master_track=ids["master_track"],
                master_car=ids["master_car"],
                session_time=base,
            )
            # Add 2 more sessions for the same driver at different
            # times, plus the matching cohort_metric rows.
            async with factory() as db:
                for offset in (1, 2):
                    sess_time = base + timedelta(hours=offset)
                    db.add(
                        SessionRow(
                            id=uuid6.uuid7(),
                            driver_id=seed["driver_id"],
                            manifest={
                                "track_id": str(ids["track_id"]),
                                "car_id": str(ids["car_id"]),
                            },
                            created_at=sess_time,
                            status="processed",
                        )
                    )
                    for dim in _DIMENSIONS:
                        db.add(
                            CohortMetric(
                                id=uuid6.uuid7(),
                                car_id=ids["master_car"],
                                track_id=ids["master_track"],
                                metric=dim, value=55.0,
                                session_time=sess_time,
                            )
                        )
                await db.commit()
            async with factory() as db:
                deleted = await purge_cohort_metrics_for_driver(
                    db, driver_id=seed["driver_id"]
                )
                await db.commit()
            assert deleted == 30
        finally:
            await engine.dispose()

    asyncio.run(run())


# -- isolation ----------------------------------------------------


def test_purge_does_not_touch_other_drivers_rows(
    upgraded_db_url: str,
) -> None:
    """alice and bob both have sessions on the same (car, track)
    at DIFFERENT times. Purging alice deletes alice's 10 rows
    only; bob's 10 remain. Cross-driver isolation."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            ids = await _seed_track_car(factory)
            alice_seed = await _seed_driver_with_session_and_metrics(
                factory, clerk_id="alice", opt_in=False,
                track_id=ids["track_id"], car_id=ids["car_id"],
                master_track=ids["master_track"],
                master_car=ids["master_car"],
                session_time=datetime(2026, 4, 26, 10, tzinfo=UTC),
            )
            await _seed_driver_with_session_and_metrics(
                factory, clerk_id="bob", opt_in=True,
                track_id=ids["track_id"], car_id=ids["car_id"],
                master_track=ids["master_track"],
                master_car=ids["master_car"],
                session_time=datetime(2026, 4, 26, 11, tzinfo=UTC),
            )
            async with factory() as db:
                deleted = await purge_cohort_metrics_for_driver(
                    db, driver_id=alice_seed["driver_id"]
                )
                await db.commit()
            assert deleted == 10
            async with factory() as db:
                remaining = (
                    (await db.execute(select(CohortMetric)))
                    .scalars().all()
                )
                # bob's 10 rows still there, alice's gone.
                assert len(remaining) == 10
                bob_session_time = datetime(
                    2026, 4, 26, 11, tzinfo=UTC,
                )
                # SQLite drops tz on round-trip — compare wall time.
                for r in remaining:
                    assert (
                        r.session_time.replace(tzinfo=UTC)
                        == bob_session_time
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())


# -- edge cases ---------------------------------------------------


def test_driver_with_no_sessions_returns_zero(
    upgraded_db_url: str,
) -> None:
    """Driver who never contributed to cohort → 0 deleted, no
    error. The opt-out flow runs unconditionally; a driver who
    was always opted-out still gets the purge attempted, and it
    must be a no-op."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            async with factory() as db:
                user = User(
                    id=uuid6.uuid7(), clerk_user_id="alice",
                    email="a@t",
                )
                driver = DriverProfile(
                    id=uuid6.uuid7(), user_id=user.id,
                    display_name="A", cohort_opt_in=False,
                )
                db.add_all([user, driver])
                await db.commit()
                deleted = await purge_cohort_metrics_for_driver(
                    db, driver_id=driver.id
                )
                await db.commit()
            assert deleted == 0
        finally:
            await engine.dispose()

    asyncio.run(run())
