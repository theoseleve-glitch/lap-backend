"""T056 (sprint2-backend) — cohort minimum (k=20) enforcement.

Per ADR-008: percentiles are published only when ≥20 distinct
opted-in drivers share a (car, track) context. This is true
k-anonymity at the driver level, not the session level — a
single driver with 20 sessions is still 1 member, not 20.

The threshold check reads from `sessions + driver_profiles +
track_layouts + car_models` (counting distinct opted-in
driver_ids), NOT from `cohort_metrics`. cohort_metrics has no
driver_id (T054 schema invariant), so the count must come from
a different query path. The privacy boundary is:
  - threshold-check INPUT: distinct driver count (aggregate)
  - threshold-check OUTPUT: bool / int (non-identifying)
  - percentile-compute INPUT: cohort_metrics (anonymized)

Acceptance:
  - count_cohort_members reflects distinct opted-in drivers
    matching master_car_id + master_track_id
  - opted-out drivers are excluded
  - one driver with N sessions contributes 1 to the count
  - is_cohort_eligible_for_percentile returns True iff count
    ≥ MIN_COHORT_SIZE (20)
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import uuid6
from alembic.config import Config as AlembicConfig

from alembic import command
from domain.cohort_minimum import (
    MIN_COHORT_SIZE,
    count_cohort_members,
    is_cohort_eligible_for_percentile,
)
from infra.db import make_engine, make_session_factory
from infra.orm import (
    CarModel,
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


async def _seed_track_and_car(
    factory: Any, *, master_track: Any, master_car: Any,
    label: str = "a",
) -> dict[str, Any]:
    """`label` distinguishes provider/name strings so the same fixture
    can seed multiple distinct (track, car) combos in one test (the
    catalog tables have a uniq constraint on provider+name)."""
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
        return {"track_id": track.id, "car_id": car.id}


async def _seed_driver_with_sessions(
    factory: Any, *, clerk_id: str, opt_in: bool,
    track_id: Any, car_id: Any, n_sessions: int = 1,
) -> Any:
    """Seed one driver with n_sessions on the given (track, car).
    Returns the driver_profile.id."""
    async with factory() as db:
        user = User(
            id=uuid6.uuid7(), clerk_user_id=clerk_id,
            email=f"{clerk_id}@t",
        )
        driver = DriverProfile(
            id=uuid6.uuid7(), user_id=user.id, display_name=clerk_id,
            cohort_opt_in=opt_in,
        )
        db.add_all([user, driver])
        for i in range(n_sessions):
            db.add(
                SessionRow(
                    id=uuid6.uuid7(), driver_id=driver.id,
                    manifest={
                        "track_id": str(track_id),
                        "car_id": str(car_id),
                    },
                    created_at=datetime(
                        2026, 4, 1 + (i // 24), (i % 24), tzinfo=UTC,
                    ),
                    status="processed",
                )
            )
        await db.commit()
        return driver.id


# -- count_cohort_members -----------------------------------------


def test_count_is_zero_when_no_drivers_have_sessions(
    upgraded_db_url: str,
) -> None:
    """No sessions on this (car, track) → count is 0."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            master_track = uuid6.uuid7()
            master_car = uuid6.uuid7()
            await _seed_track_and_car(
                factory, master_track=master_track, master_car=master_car,
            )
            async with factory() as db:
                n = await count_cohort_members(
                    db, car_id=master_car, track_id=master_track,
                )
            assert n == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_count_includes_each_opted_in_driver_once(
    upgraded_db_url: str,
) -> None:
    """3 opted-in drivers, each with 1 session → count is 3."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            master_track = uuid6.uuid7()
            master_car = uuid6.uuid7()
            ids = await _seed_track_and_car(
                factory, master_track=master_track, master_car=master_car,
            )
            for i in range(3):
                await _seed_driver_with_sessions(
                    factory, clerk_id=f"d{i}", opt_in=True,
                    track_id=ids["track_id"], car_id=ids["car_id"],
                )
            async with factory() as db:
                n = await count_cohort_members(
                    db, car_id=master_car, track_id=master_track,
                )
            assert n == 3
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_count_excludes_opted_out_drivers(upgraded_db_url: str) -> None:
    """5 opted-in + 3 opted-out → count is 5. Privacy gate."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            master_track = uuid6.uuid7()
            master_car = uuid6.uuid7()
            ids = await _seed_track_and_car(
                factory, master_track=master_track, master_car=master_car,
            )
            for i in range(5):
                await _seed_driver_with_sessions(
                    factory, clerk_id=f"in{i}", opt_in=True,
                    track_id=ids["track_id"], car_id=ids["car_id"],
                )
            for i in range(3):
                await _seed_driver_with_sessions(
                    factory, clerk_id=f"out{i}", opt_in=False,
                    track_id=ids["track_id"], car_id=ids["car_id"],
                )
            async with factory() as db:
                n = await count_cohort_members(
                    db, car_id=master_car, track_id=master_track,
                )
            assert n == 5
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_one_driver_with_many_sessions_counts_once(
    upgraded_db_url: str,
) -> None:
    """k-anonymity at the DRIVER level: a single driver with 50
    sessions still contributes 1 to the count, not 50. Without
    DISTINCT this would be 50 — that's the bug this test guards."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            master_track = uuid6.uuid7()
            master_car = uuid6.uuid7()
            ids = await _seed_track_and_car(
                factory, master_track=master_track, master_car=master_car,
            )
            await _seed_driver_with_sessions(
                factory, clerk_id="prolific", opt_in=True,
                track_id=ids["track_id"], car_id=ids["car_id"],
                n_sessions=50,
            )
            async with factory() as db:
                n = await count_cohort_members(
                    db, car_id=master_car, track_id=master_track,
                )
            assert n == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_count_is_per_car_track_combo(upgraded_db_url: str) -> None:
    """Drivers on a different (car, track) combo don't count
    toward this cohort. Pins the WHERE clause."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            mt_a = uuid6.uuid7()
            mc_a = uuid6.uuid7()
            mt_b = uuid6.uuid7()
            mc_b = uuid6.uuid7()
            a = await _seed_track_and_car(
                factory, master_track=mt_a, master_car=mc_a, label="a",
            )
            b = await _seed_track_and_car(
                factory, master_track=mt_b, master_car=mc_b, label="b",
            )
            for i in range(7):
                await _seed_driver_with_sessions(
                    factory, clerk_id=f"a{i}", opt_in=True,
                    track_id=a["track_id"], car_id=a["car_id"],
                )
            for i in range(2):
                await _seed_driver_with_sessions(
                    factory, clerk_id=f"b{i}", opt_in=True,
                    track_id=b["track_id"], car_id=b["car_id"],
                )
            async with factory() as db:
                n_a = await count_cohort_members(
                    db, car_id=mc_a, track_id=mt_a,
                )
                n_b = await count_cohort_members(
                    db, car_id=mc_b, track_id=mt_b,
                )
            assert n_a == 7
            assert n_b == 2
        finally:
            await engine.dispose()

    asyncio.run(run())


# -- is_cohort_eligible_for_percentile ----------------------------


def test_eligible_at_threshold(upgraded_db_url: str) -> None:
    """Exactly MIN_COHORT_SIZE drivers → eligible (≥ is inclusive)."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            master_track = uuid6.uuid7()
            master_car = uuid6.uuid7()
            ids = await _seed_track_and_car(
                factory, master_track=master_track, master_car=master_car,
            )
            for i in range(MIN_COHORT_SIZE):
                await _seed_driver_with_sessions(
                    factory, clerk_id=f"d{i}", opt_in=True,
                    track_id=ids["track_id"], car_id=ids["car_id"],
                )
            async with factory() as db:
                ok = await is_cohort_eligible_for_percentile(
                    db, car_id=master_car, track_id=master_track,
                )
            assert ok is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_not_eligible_below_threshold(upgraded_db_url: str) -> None:
    """One short of MIN_COHORT_SIZE → not eligible."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            master_track = uuid6.uuid7()
            master_car = uuid6.uuid7()
            ids = await _seed_track_and_car(
                factory, master_track=master_track, master_car=master_car,
            )
            for i in range(MIN_COHORT_SIZE - 1):
                await _seed_driver_with_sessions(
                    factory, clerk_id=f"d{i}", opt_in=True,
                    track_id=ids["track_id"], car_id=ids["car_id"],
                )
            async with factory() as db:
                ok = await is_cohort_eligible_for_percentile(
                    db, car_id=master_car, track_id=master_track,
                )
            assert ok is False
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_not_eligible_when_empty(upgraded_db_url: str) -> None:
    """Zero contributors → not eligible. Edge case for unknown
    (car, track) tuples — no fallback to True."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            unknown_track = uuid6.uuid7()
            unknown_car = uuid6.uuid7()
            async with factory() as db:
                ok = await is_cohort_eligible_for_percentile(
                    db, car_id=unknown_car, track_id=unknown_track,
                )
            assert ok is False
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_min_cohort_size_is_20() -> None:
    """ADR-008 pins MIN_COHORT_SIZE=20. This is a constant the
    rest of the system reads — pinning it here so a silent
    change to e.g. 10 trips a test rather than weakens privacy."""
    assert MIN_COHORT_SIZE == 20
