"""T057 (sprint2-backend) — percentile compute.

Per ADR-008: once a cohort has >=20 distinct opted-in drivers
(T056), publish percentiles for that (car, track, metric).
T057 delivers the math + the cohort fetch glue.

Two layers:
  1. compute_percentile_rank(cohort_values, value) -> float
     Pure function. Mid-rank convention (handles ties
     symmetrically): rank = (#<value + 0.5 * #==value) / N.
     Returns 0.0..1.0.
  2. cohort_percentile_for_value(db, *, car_id, track_id,
     metric, value) -> float | None
     Reads cohort_metrics with the master IDs, applies the
     T056 eligibility gate, and returns None if the cohort
     hasn't unlocked yet (so callers must handle the null).

The midrank convention matters for sparse cohorts where many
drivers cluster at common values: it's fair to ties (50/50
split) rather than asymmetric (strict < gives the lower side
the win).
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import uuid6
from alembic.config import Config as AlembicConfig

from alembic import command
from domain.percentile_compute import (
    cohort_percentile_for_value,
    compute_percentile_rank,
)
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


# -- pure math: compute_percentile_rank ---------------------------


def test_rank_is_zero_when_value_below_all() -> None:
    """Value strictly less than every cohort value → rank 0."""
    assert compute_percentile_rank([10, 20, 30], 5) == 0.0


def test_rank_is_one_when_value_above_all() -> None:
    """Value strictly greater than every cohort value → rank 1.0."""
    assert compute_percentile_rank([10, 20, 30], 100) == 1.0


def test_rank_is_half_at_median() -> None:
    """For [10, 20, 30] and value 20 (the median), midrank
    convention gives (#<20 + 0.5*#==20)/3 = (1 + 0.5*1)/3 = 0.5."""
    assert math.isclose(compute_percentile_rank([10, 20, 30], 20), 0.5)


def test_rank_handles_ties_symmetrically() -> None:
    """5 values, 3 of which equal the query value: midrank gives
    (#strictly_below + 0.5 * #equal) / N = (1 + 0.5*3)/5 = 0.5.
    Plain '<' convention would give 1/5=0.2 — too punitive to ties.
    Plain '<=' would give 4/5=0.8 — too generous. Midrank is the
    fair middle."""
    assert math.isclose(
        compute_percentile_rank([5, 10, 10, 10, 15], 10), 0.5
    )


def test_rank_close_to_one_for_largest_unique() -> None:
    """Largest unique value: (#<v + 0.5*1)/N = ((N-1) + 0.5)/N.
    For N=4, (3 + 0.5)/4 = 0.875."""
    assert math.isclose(
        compute_percentile_rank([10, 20, 30, 40], 40), 0.875
    )


def test_rank_raises_on_empty_cohort() -> None:
    """An empty cohort can't yield a meaningful percentile —
    callers must handle this earlier (via the T056 gate). We
    refuse rather than silently returning 0.0 or 1.0."""
    with pytest.raises(ValueError):
        compute_percentile_rank([], 10)


# -- glue: cohort_percentile_for_value ----------------------------


async def _seed_track_car_and_drivers(
    factory: Any, *, master_track: Any, master_car: Any,
    n_opt_in: int, label: str = "a",
) -> dict[str, Any]:
    """Seed a (track, car) and n_opt_in drivers each with one
    session — used to satisfy the T056 cohort minimum.

    `label` distinguishes provider/name strings so multiple
    (track, car) seeds can co-exist in one test (catalog
    uniq constraint on provider+name); also used for the
    clerk_user_id to keep User rows unique.
    """
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
        for i in range(n_opt_in):
            user = User(
                id=uuid6.uuid7(), clerk_user_id=f"{label}u{i}",
                email=f"{label}u{i}@t",
            )
            driver = DriverProfile(
                id=uuid6.uuid7(), user_id=user.id,
                display_name=f"{label}D{i}", cohort_opt_in=True,
            )
            session = SessionRow(
                id=uuid6.uuid7(), driver_id=driver.id,
                manifest={
                    "track_id": str(track.id), "car_id": str(car.id),
                },
                created_at=datetime(2026, 4, 26, tzinfo=UTC),
                status="processed",
            )
            db.add_all([user, driver, session])
        await db.commit()
        return {"track_id": track.id, "car_id": car.id}


async def _seed_cohort_metrics(
    factory: Any, *, master_track: Any, master_car: Any,
    metric: str, values: list[float],
) -> None:
    async with factory() as db:
        for v in values:
            db.add(
                CohortMetric(
                    id=uuid6.uuid7(),
                    car_id=master_car, track_id=master_track,
                    metric=metric, value=v,
                    session_time=datetime(2026, 4, 26, tzinfo=UTC),
                )
            )
        await db.commit()


def test_returns_none_when_cohort_below_minimum(
    upgraded_db_url: str,
) -> None:
    """Cohort has 5 opt-in drivers (well below MIN=20) → None.
    Privacy gate: don't publish percentiles for tiny cohorts
    even if cohort_metrics rows exist."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            mt = uuid6.uuid7()
            mc = uuid6.uuid7()
            await _seed_track_car_and_drivers(
                factory, master_track=mt, master_car=mc, n_opt_in=5,
            )
            await _seed_cohort_metrics(
                factory, master_track=mt, master_car=mc,
                metric="braking", values=[10.0, 20.0, 30.0, 40.0, 50.0],
            )
            async with factory() as db:
                rank = await cohort_percentile_for_value(
                    db, car_id=mc, track_id=mt,
                    metric="braking", value=25.0,
                )
            assert rank is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_returns_rank_when_cohort_meets_minimum(
    upgraded_db_url: str,
) -> None:
    """20 opt-in drivers → cohort eligible → rank returned.
    Pin both the eligibility gate AND the rank value."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            mt = uuid6.uuid7()
            mc = uuid6.uuid7()
            await _seed_track_car_and_drivers(
                factory, master_track=mt, master_car=mc, n_opt_in=20,
            )
            await _seed_cohort_metrics(
                factory, master_track=mt, master_car=mc,
                metric="braking",
                values=[float(x) for x in range(0, 100, 5)],  # 0..95 step 5
            )
            async with factory() as db:
                rank = await cohort_percentile_for_value(
                    db, car_id=mc, track_id=mt,
                    metric="braking", value=50.0,
                )
            # 50.0 is in the cohort exactly once. #<50 = 10 (0..45),
            # #==50 = 1, N = 20. Midrank = (10 + 0.5)/20 = 0.525.
            assert rank is not None
            assert math.isclose(rank, 0.525)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_filters_by_metric_name(upgraded_db_url: str) -> None:
    """Cohort metrics for OTHER metrics must not pollute the
    distribution — only the requested metric's rows count."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            mt = uuid6.uuid7()
            mc = uuid6.uuid7()
            await _seed_track_car_and_drivers(
                factory, master_track=mt, master_car=mc, n_opt_in=20,
            )
            # Same (car, track), two metrics. We query one of them.
            await _seed_cohort_metrics(
                factory, master_track=mt, master_car=mc,
                metric="braking", values=[100.0] * 5,
            )
            await _seed_cohort_metrics(
                factory, master_track=mt, master_car=mc,
                metric="racecraft", values=[0.0] * 5,
            )
            async with factory() as db:
                # value below all braking values → rank 0.0
                rank = await cohort_percentile_for_value(
                    db, car_id=mc, track_id=mt,
                    metric="braking", value=10.0,
                )
            assert rank == 0.0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_filters_by_master_ids(upgraded_db_url: str) -> None:
    """A different (car, track) cohort must not contribute to the
    queried distribution. Pins the WHERE clause on master IDs."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            mt_a = uuid6.uuid7()
            mc_a = uuid6.uuid7()
            mt_b = uuid6.uuid7()
            mc_b = uuid6.uuid7()
            await _seed_track_car_and_drivers(
                factory, master_track=mt_a, master_car=mc_a,
                n_opt_in=20, label="a",
            )
            await _seed_track_car_and_drivers(
                factory, master_track=mt_b, master_car=mc_b,
                n_opt_in=20, label="b",
            )
            await _seed_cohort_metrics(
                factory, master_track=mt_a, master_car=mc_a,
                metric="braking", values=[100.0] * 5,
            )
            await _seed_cohort_metrics(
                factory, master_track=mt_b, master_car=mc_b,
                metric="braking", values=[0.0] * 5,
            )
            async with factory() as db:
                # Querying cohort A, value=10.0 — A's values are all
                # 100.0, so rank is 0.0 (well below). If B leaked in,
                # we'd see >0.
                rank = await cohort_percentile_for_value(
                    db, car_id=mc_a, track_id=mt_a,
                    metric="braking", value=10.0,
                )
            assert rank == 0.0
        finally:
            await engine.dispose()

    asyncio.run(run())
