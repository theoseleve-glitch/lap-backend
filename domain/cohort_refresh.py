"""T063 (sprint2-backend) — weekly cohort refresh.

Per ADR-008, percentiles are recomputed on a periodic schedule
so DriverBenchmarkProfile rows stay current as the cohort
grows / shifts. This module's `refresh_driver_benchmark_profile`
is the per-driver unit of work; the celery beat in
infra.celery_app schedules it weekly across all opt-in drivers.

Scope: per-driver, single (car, track) cohort. The driver's
FIRST eligible (car, track) — the one most recently played
where the cohort has unlocked — produces their persisted
profile. Multi-cohort aggregation is a future T (avoid
designing for it here).

Privacy gates inherited:
  - Driver opt-in check (T052)
  - Cohort minimum (T056) — locked cohort → no profile
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

import uuid6
from sqlalchemy import select

from domain.cohort_eligibility import is_eligible_for_cohort
from domain.cohort_minimum import (
    count_cohort_members,
    is_cohort_eligible_for_percentile,
)
from domain.percentile_compute import cohort_percentile_for_value
from infra.orm import (
    CarModel,
    DriverBenchmarkProfile,
    DriverProfile,
    DriverProgressSnapshot,
    TrackLayout,
)
from infra.orm import Session as SessionRow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_DIMENSIONS = (
    "braking", "throttle_pickup", "minimum_speed", "line_consistency",
    "tire_management", "racecraft", "fatigue_consistency",
    "strategy_execution", "setup_sensitivity", "adaptation",
)


async def refresh_driver_benchmark_profile(
    db: AsyncSession,
    *,
    driver_id: UUID,
) -> DriverBenchmarkProfile | None:
    """Compute and persist the driver's percentile profile, or
    None if the driver is opted out or no eligible cohort exists.

    Idempotent: re-running on a driver who already has a profile
    UPDATES the row in place (single profile per driver).
    """
    profile = (
        await db.execute(
            select(DriverProfile).where(DriverProfile.id == driver_id)
        )
    ).scalar_one_or_none()
    if profile is None or not is_eligible_for_cohort(profile.cohort_opt_in):
        return None

    snapshot = (
        await db.execute(
            select(DriverProgressSnapshot)
            .where(DriverProgressSnapshot.driver_id == driver_id)
            .order_by(DriverProgressSnapshot.captured_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if snapshot is None:
        return None

    sessions = (
        (
            await db.execute(
                select(SessionRow)
                .where(SessionRow.driver_id == driver_id)
                .order_by(SessionRow.created_at.desc())
            )
        )
        .scalars()
        .all()
    )

    # Find the first session whose (car, track) cohort is eligible.
    chosen: tuple[UUID, UUID] | None = None
    for session in sessions:
        manifest = session.manifest or {}
        raw_track = manifest.get("track_id")
        raw_car = manifest.get("car_id")
        if not raw_track or not raw_car:
            continue
        try:
            track_pk = UUID(str(raw_track))
            car_pk = UUID(str(raw_car))
        except ValueError:
            continue
        track = (
            await db.execute(
                select(TrackLayout).where(TrackLayout.id == track_pk)
            )
        ).scalar_one_or_none()
        car = (
            await db.execute(
                select(CarModel).where(CarModel.id == car_pk)
            )
        ).scalar_one_or_none()
        if track is None or car is None:
            continue
        if await is_cohort_eligible_for_percentile(
            db, car_id=car.master_car_id, track_id=track.master_track_id
        ):
            chosen = (car.master_car_id, track.master_track_id)
            break

    if chosen is None:
        return None

    car_master, track_master = chosen
    per_dim: dict[str, float] = {}
    for dim_name in _DIMENSIONS:
        dim = getattr(snapshot, dim_name)
        score = dim.get("score") if isinstance(dim, dict) else None
        if score is None:
            continue
        rank = await cohort_percentile_for_value(
            db,
            car_id=car_master, track_id=track_master,
            metric=dim_name, value=float(score),
        )
        if rank is not None:
            per_dim[dim_name] = rank

    cohort_size = await count_cohort_members(
        db, car_id=car_master, track_id=track_master
    )

    cohort_def = {
        "car_id": str(car_master),
        "track_id": str(track_master),
    }
    now = datetime.now(UTC)

    existing = (
        await db.execute(
            select(DriverBenchmarkProfile).where(
                DriverBenchmarkProfile.driver_id == driver_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.generated_at = now
        existing.cohort_definition = cohort_def
        existing.cohort_size = cohort_size
        existing.per_dimension_percentiles = per_dim
        return existing

    new_row = DriverBenchmarkProfile(
        id=uuid6.uuid7(),
        driver_id=driver_id,
        generated_at=now,
        cohort_definition=cohort_def,
        cohort_size=cohort_size,
        per_track_car_percentiles={},
        per_dimension_percentiles=per_dim,
    )
    db.add(new_row)
    return new_row
