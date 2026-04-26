"""T063 (sprint2-backend) — weekly cohort refresh.

Cron-driven recomputation of DriverBenchmarkProfile rows. Per
ADR-008, percentiles publish only when the cohort is unlocked
(T056); the refresh job iterates opt-in drivers and persists
a benchmark profile for each one whose first eligible (car,
track) cohort has unlocked.

Two layers:
  - refresh_driver_benchmark_profile(db, *, driver_id): per-
    driver persistence (testable in isolation)
  - infra.celery_app beat_schedule entry "weekly-cohort-refresh"
    fires the job (verified by schedule presence)

Acceptance:
  - opt-in driver, eligible cohort, has snapshot → row created
  - opt-out driver → no row, returns None (privacy gate)
  - cohort below threshold → no row, returns None
  - re-run on existing profile → updated in place (idempotent)
  - schedule entry exists in celery beat config
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
from domain.cohort_refresh import refresh_driver_benchmark_profile
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


async def _seed_driver_with_eligible_cohort(
    factory: Any, *, opt_in: bool = True, n_cohort: int = 20,
) -> dict[str, Any]:
    """Seed a target driver + 20 cohort drivers + cohort_metric
    rows so the (car, track) cohort meets MIN_COHORT_SIZE."""
    master_track = uuid6.uuid7()
    master_car = uuid6.uuid7()
    async with factory() as db:
        track = TrackLayout(
            id=uuid6.uuid7(), source_provider="iracing",
            name="Spa", layout_name="GP",
            start_finish_lat=50.4, start_finish_lon=5.97,
            start_finish_heading_deg=10.0,
            sector_checkpoints=[], corner_names=[],
            master_track_id=master_track,
        )
        car = CarModel(
            id=uuid6.uuid7(), source_provider="iracing",
            name="911", car_class="gt3",
            master_car_id=master_car,
        )
        db.add_all([track, car])
        await db.commit()

        # Target driver — alice — with opt_in flag and snapshot.
        alice_user = User(
            id=uuid6.uuid7(), clerk_user_id="alice", email="a@t",
        )
        alice = DriverProfile(
            id=uuid6.uuid7(), user_id=alice_user.id,
            display_name="Alice", cohort_opt_in=opt_in,
        )
        alice_session = SessionRow(
            id=uuid6.uuid7(), driver_id=alice.id,
            manifest={
                "track_id": str(track.id), "car_id": str(car.id),
            },
            created_at=datetime(2026, 4, 26, 10, tzinfo=UTC),
            status="processed",
        )
        snap = DriverProgressSnapshot(
            id=uuid6.uuid7(), driver_id=alice.id,
            captured_at=datetime(2026, 4, 26, tzinfo=UTC),
            window="session",
            window_start=datetime(2026, 4, 26, tzinfo=UTC),
            window_end=datetime(2026, 4, 26, tzinfo=UTC),
            braking=_dim(50.0),
            throttle_pickup=_dim(60.0), minimum_speed=_dim(60.0),
            line_consistency=_dim(60.0), tire_management=_dim(60.0),
            racecraft=_dim(60.0), fatigue_consistency=_dim(60.0),
            strategy_execution=_dim(60.0),
            setup_sensitivity=_dim(60.0), adaptation=_dim(60.0),
            overall_score=60.0, overall_confidence=0.7,
            generated_by="t063-test",
        )
        db.add_all([alice_user, alice, alice_session, snap])

        # Pad cohort to n_cohort distinct opt-in drivers (with
        # one session each) so T056 eligibility passes.
        for i in range(n_cohort):
            user = User(
                id=uuid6.uuid7(), clerk_user_id=f"pad{i}",
                email=f"pad{i}@t",
            )
            driver = DriverProfile(
                id=uuid6.uuid7(), user_id=user.id,
                display_name=f"P{i}", cohort_opt_in=True,
            )
            session = SessionRow(
                id=uuid6.uuid7(), driver_id=driver.id,
                manifest={
                    "track_id": str(track.id),
                    "car_id": str(car.id),
                },
                created_at=datetime(2026, 4, 26, 11, tzinfo=UTC),
                status="processed",
            )
            db.add_all([user, driver, session])

        # Cohort metrics: 20 braking values 0..95 step 5 +
        # alice's 50 elsewhere. Spread enough to compute rank.
        for v in (float(x) for x in range(0, 100, 5)):
            db.add(
                CohortMetric(
                    id=uuid6.uuid7(),
                    car_id=master_car, track_id=master_track,
                    metric="braking", value=v,
                    session_time=datetime(2026, 4, 26, 11, tzinfo=UTC),
                )
            )
        await db.commit()
        return {
            "alice_id": alice.id,
            "master_track": master_track,
            "master_car": master_car,
        }


# -- happy paths --------------------------------------------------


def test_refresh_creates_profile_for_eligible_driver(
    upgraded_db_url: str,
) -> None:
    """Opt-in driver + eligible cohort + snapshot → a
    DriverBenchmarkProfile row is created with non-empty
    per_dimension_percentiles."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            seed = await _seed_driver_with_eligible_cohort(factory)
            async with factory() as db:
                profile = await refresh_driver_benchmark_profile(
                    db, driver_id=seed["alice_id"]
                )
                await db.commit()
            assert profile is not None
            async with factory() as db:
                rows = (
                    (await db.execute(select(DriverBenchmarkProfile)))
                    .scalars().all()
                )
                assert len(rows) == 1
                assert rows[0].driver_id == seed["alice_id"]
                assert "braking" in rows[0].per_dimension_percentiles
                assert rows[0].cohort_size >= 20
        finally:
            await engine.dispose()

    asyncio.run(run())


# -- privacy + locked-cohort gates --------------------------------


def test_refresh_returns_none_for_opted_out_driver(
    upgraded_db_url: str,
) -> None:
    """Driver with cohort_opt_in=False → None. Privacy gate
    prevents persisting a profile they didn't consent to."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            seed = await _seed_driver_with_eligible_cohort(
                factory, opt_in=False,
            )
            async with factory() as db:
                profile = await refresh_driver_benchmark_profile(
                    db, driver_id=seed["alice_id"]
                )
                await db.commit()
            assert profile is None
            async with factory() as db:
                rows = (
                    (await db.execute(select(DriverBenchmarkProfile)))
                    .scalars().all()
                )
                assert rows == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_refresh_returns_none_when_cohort_below_threshold(
    upgraded_db_url: str,
) -> None:
    """Eligible driver but cohort has only 5 members → None.
    Don't persist a profile for a locked cohort."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            seed = await _seed_driver_with_eligible_cohort(
                factory, n_cohort=5,
            )
            async with factory() as db:
                profile = await refresh_driver_benchmark_profile(
                    db, driver_id=seed["alice_id"]
                )
                await db.commit()
            assert profile is None
        finally:
            await engine.dispose()

    asyncio.run(run())


# -- idempotency --------------------------------------------------


def test_refresh_updates_existing_profile_in_place(
    upgraded_db_url: str,
) -> None:
    """Re-running refresh on a driver who already has a profile
    UPDATES the row rather than inserting a duplicate. Cron
    runs weekly; a duplicating refresh would explode the table."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            seed = await _seed_driver_with_eligible_cohort(factory)
            async with factory() as db:
                first = await refresh_driver_benchmark_profile(
                    db, driver_id=seed["alice_id"]
                )
                await db.commit()
            assert first is not None
            async with factory() as db:
                second = await refresh_driver_benchmark_profile(
                    db, driver_id=seed["alice_id"]
                )
                await db.commit()
            assert second is not None
            async with factory() as db:
                rows = (
                    (await db.execute(select(DriverBenchmarkProfile)))
                    .scalars().all()
                )
                assert len(rows) == 1  # not duplicated
        finally:
            await engine.dispose()

    asyncio.run(run())


# -- celery beat schedule -----------------------------------------


def test_weekly_cohort_refresh_is_in_beat_schedule() -> None:
    """The weekly refresh task is registered in celery beat
    so it actually fires in production. Pin the schedule slot
    so a future PR that drops the entry trips this."""
    from infra.celery_app import celery_app
    schedule = celery_app.conf.beat_schedule
    assert "weekly-cohort-refresh" in schedule
    entry = schedule["weekly-cohort-refresh"]
    assert "schedule" in entry
    assert "task" in entry
