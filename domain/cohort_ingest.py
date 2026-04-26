"""T055 (sprint2-backend) — cohort metric ingestion.

When a session finishes processing AND the driver is opted in,
append one row per skill dimension to `cohort_metrics`. The
privacy gate is enforced HERE — callers do not check opt-in
themselves, because a forgotten check would silently leak data.
The ingestion function takes the session+snapshot and returns
the row count actually written (0 for opted-out drivers; 10 for
opted-in if all dimensions present).

The rows are anonymized at write time:
  - car_id = master_car_id (cross-provider equivalence ID)
  - track_id = master_track_id
  - metric = dimension name (e.g., "braking")
  - value = dimension.score (the 0-100 ability score)
  - session_time = session.created_at
  - NO driver_id (the schema has no column for it)

If the session manifest is missing track_id/car_id, or those
IDs don't resolve to catalog rows, ingestion is skipped (0
rows). This matches the existing bridge.py "session_unbridgeable"
posture: don't crash the batch, just skip.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import uuid6
from sqlalchemy import select

from domain.cohort_eligibility import is_eligible_for_cohort
from infra.orm import (
    CarModel,
    CohortMetric,
    DriverProfile,
    TrackLayout,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from infra.orm import DriverProgressSnapshot
    from infra.orm import Session as SessionRow


_DIMENSIONS: tuple[str, ...] = (
    "braking",
    "throttle_pickup",
    "minimum_speed",
    "line_consistency",
    "tire_management",
    "racecraft",
    "fatigue_consistency",
    "strategy_execution",
    "setup_sensitivity",
    "adaptation",
    # T078 — long-run benchmark. pace_decay is nullable on the
    # snapshot (T073), so sprint-only drivers contribute no row
    # via the existing isinstance(dim, dict) guard below.
    "pace_decay",
)


async def ingest_cohort_metrics(
    db: AsyncSession,
    *,
    session: SessionRow,
    progress_snapshot: DriverProgressSnapshot,
) -> int:
    """Append cohort_metric rows for one (session, snapshot) pair.

    Returns the number of rows appended:
      - 0 if the driver is not opted in (privacy gate)
      - 0 if manifest is missing track_id/car_id or catalog
        rows can't be resolved (skip-not-error posture)
      - len(_DIMENSIONS) on success
    """
    profile = (
        await db.execute(
            select(DriverProfile).where(
                DriverProfile.id == session.driver_id
            )
        )
    ).scalar_one_or_none()
    if profile is None or not is_eligible_for_cohort(profile.cohort_opt_in):
        return 0

    manifest = session.manifest or {}
    raw_track = manifest.get("track_id")
    raw_car = manifest.get("car_id")
    if not raw_track or not raw_car:
        return 0
    try:
        track_id = UUID(str(raw_track))
        car_id = UUID(str(raw_car))
    except ValueError:
        return 0

    track = (
        await db.execute(
            select(TrackLayout).where(TrackLayout.id == track_id)
        )
    ).scalar_one_or_none()
    car = (
        await db.execute(
            select(CarModel).where(CarModel.id == car_id)
        )
    ).scalar_one_or_none()
    if track is None or car is None:
        return 0

    written = 0
    for dim_name in _DIMENSIONS:
        dim = getattr(progress_snapshot, dim_name)
        score = dim.get("score") if isinstance(dim, dict) else None
        if score is None:
            continue
        db.add(
            CohortMetric(
                id=uuid6.uuid7(),
                car_id=car.master_car_id,
                track_id=track.master_track_id,
                metric=dim_name,
                value=float(score),
                session_time=session.created_at,
            )
        )
        written += 1
    return written
