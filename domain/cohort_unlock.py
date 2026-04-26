"""T065 (sprint2-backend) — cohort unlock notification trigger.

When a (master_car, master_track) cohort crosses the k=20
threshold (T056), this trigger fans out T063's per-driver
refresh across every opt-in driver who has contributed to that
cohort. The "event" the spec calls out is the function call
itself; idempotency comes from T063's upsert, so re-firing on
an already-unlocked cohort is harmless and keeps profiles
fresh as new sessions land.

Composition:
  - is_cohort_eligible_for_percentile (T056) — the unlock gate
  - refresh_driver_benchmark_profile (T063) — per-driver work,
    idempotent upsert into DriverBenchmarkProfile

Why fan out to every contributor: when the cohort transitions
from locked → unlocked, every existing contributor's percentile
becomes computable for the first time. They should all see a
profile populate together, not just the driver whose ingestion
crossed the line.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from domain.cohort_minimum import is_cohort_eligible_for_percentile
from domain.cohort_refresh import refresh_driver_benchmark_profile
from infra.orm import (
    CarModel,
    DriverProfile,
    TrackLayout,
)
from infra.orm import Session as SessionRow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def process_cohort_unlock(
    db: AsyncSession,
    *,
    car_id: UUID,
    track_id: UUID,
) -> list[UUID]:
    """Trigger benchmark population for the (car, track) cohort.

    Returns the ids of drivers whose profiles were created or
    updated. Empty list if the cohort hasn't crossed the
    threshold yet.

    Idempotent: re-firing on an already-unlocked cohort updates
    rows in place via T063's upsert; row count stays bounded
    even if invoked on every subsequent ingest.
    """
    if not await is_cohort_eligible_for_percentile(
        db, car_id=car_id, track_id=track_id
    ):
        return []

    # Distinct opt-in drivers with at least one session matching
    # this (master_car, master_track). Reuses the same join shape
    # as T056's count, but returns ids instead of count.
    track_master = {
        t.id: t.master_track_id
        for t in (await db.execute(select(TrackLayout))).scalars()
    }
    car_master = {
        c.id: c.master_car_id
        for c in (await db.execute(select(CarModel))).scalars()
    }

    sessions = (
        await db.execute(
            select(SessionRow.driver_id, SessionRow.manifest)
            .join(
                DriverProfile, DriverProfile.id == SessionRow.driver_id
            )
            .where(DriverProfile.cohort_opt_in.is_(True))
        )
    ).all()

    contributors: set[UUID] = set()
    for driver_id, manifest in sessions:
        m = manifest or {}
        raw_track = m.get("track_id")
        raw_car = m.get("car_id")
        if not raw_track or not raw_car:
            continue
        try:
            t = UUID(str(raw_track))
            c = UUID(str(raw_car))
        except ValueError:
            continue
        if track_master.get(t) == track_id and car_master.get(c) == car_id:
            contributors.add(driver_id)

    refreshed: list[UUID] = []
    for driver_id in contributors:
        row = await refresh_driver_benchmark_profile(
            db, driver_id=driver_id
        )
        if row is not None:
            refreshed.append(driver_id)
    return refreshed
