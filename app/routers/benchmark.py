"""T058 (sprint2-backend) — GET /api/v1/driver/benchmark.

Returns the authenticated driver's percentile rank in the cohort
for the requested (car, track) context. Per ADR-008, percentiles
publish only when the cohort has unlocked (>=20 distinct opted-in
drivers per T056); otherwise `percentiles` is null and the client
displays the "Cohort unlocks at 20 comparable drivers" message.

The driver's value for each dimension comes from their LATEST
DriverProgressSnapshot. We pull every dimension from the snapshot
and rank each one against its corresponding cohort distribution.

Cohort gate is checked once at the top — if not eligible, return
null before doing any per-dimension work. Saves O(N_dimensions)
DB roundtrips for the common "cohort hasn't unlocked" path.

404 if the auth user has no DriverProfile row (mirror T053).
422 if car_id/track_id query params are missing or malformed
(FastAPI default validation).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from lap_schemas.v1 import CohortSizeResponse, DriverBenchmarkResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from app.error_codes import ErrorCode
from domain.cohort_minimum import (
    MIN_COHORT_SIZE,
    count_cohort_members,
    is_cohort_eligible_for_percentile,
)
from domain.percentile_compute import cohort_percentile_for_value
from infra.orm import DriverProfile, DriverProgressSnapshot
from infra.orm import User as UserRow

router = APIRouter(prefix="/api/v1/driver", tags=["driver-benchmark"])

_DIMENSIONS = (
    "braking", "throttle_pickup", "minimum_speed", "line_consistency",
    "tire_management", "racecraft", "fatigue_consistency",
    "strategy_execution", "setup_sensitivity", "adaptation",
)


@router.get(
    "/benchmark",
    response_model=DriverBenchmarkResponse,
    responses={404: {"description": "driver_not_found"}},
)
async def get_driver_benchmark(
    car_id: Annotated[UUID, Query()],
    track_id: Annotated[UUID, Query()],
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DriverBenchmarkResponse:
    profile = (
        await db.execute(
            select(DriverProfile).where(DriverProfile.user_id == user.id)
        )
    ).scalar_one_or_none()
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorCode.DRIVER_NOT_FOUND,
        )

    # Top-level eligibility gate: if locked, short-circuit to null.
    eligible = await is_cohort_eligible_for_percentile(
        db, car_id=car_id, track_id=track_id
    )
    if not eligible:
        return DriverBenchmarkResponse(
            car_id=car_id, track_id=track_id, percentiles=None,
        )

    # Latest snapshot for the auth'd driver — values to rank.
    snapshot = (
        await db.execute(
            select(DriverProgressSnapshot)
            .where(DriverProgressSnapshot.driver_id == profile.id)
            .order_by(DriverProgressSnapshot.captured_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    percentiles: dict[str, float] = {}
    if snapshot is not None:
        for dim_name in _DIMENSIONS:
            dim = getattr(snapshot, dim_name)
            score = dim.get("score") if isinstance(dim, dict) else None
            if score is None:
                continue
            rank = await cohort_percentile_for_value(
                db,
                car_id=car_id, track_id=track_id,
                metric=dim_name, value=float(score),
            )
            if rank is not None:
                percentiles[dim_name] = rank

    return DriverBenchmarkResponse(
        car_id=car_id, track_id=track_id, percentiles=percentiles,
    )


# T059 — companion to /benchmark for the "X / 20 drivers" UI
# message when /benchmark returns null percentiles. Reports the
# raw count regardless of unlock state so the client can render
# progress; the unlock_threshold field is server-controlled so
# the UI doesn't need to hardcode k=20.
@router.get(
    "/cohort_size",
    response_model=CohortSizeResponse,
)
async def get_cohort_size(
    car_id: Annotated[UUID, Query()],
    track_id: Annotated[UUID, Query()],
    _user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CohortSizeResponse:
    n = await count_cohort_members(db, car_id=car_id, track_id=track_id)
    return CohortSizeResponse(
        car_id=car_id, track_id=track_id,
        cohort_size=n, unlock_threshold=MIN_COHORT_SIZE,
    )
