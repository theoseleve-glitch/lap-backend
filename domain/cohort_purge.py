"""T062 (sprint2-backend) — cohort data deletion on opt-out.

When a driver flips cohort_opt_in False (T053), call this to
purge their previously ingested cohort_metric rows. Per ADR-008:
opt-out implies "their data is gone", but the driver never
appears as a column in cohort_metrics (T054 invariant), so the
deletion path re-derives the attribution from the sessions
table.

Algorithm:
  1. Fetch the driver's sessions (driver_id is here, not in
     cohort_metrics).
  2. For each session, resolve manifest.{track_id, car_id}
     to (master_track_id, master_car_id).
  3. DELETE FROM cohort_metrics WHERE car_id=? AND track_id=?
     AND session_time = session.created_at.

Why session_time as the disambiguator: T055 ingestion sets
cohort_metric.session_time = session.created_at exactly. With
microsecond-precision timestamps, two different drivers'
sessions on the same (car, track) won't collide. The natural
join key for "this row came from THIS session" is the
(car, track, session_time) triple.

Caveat: if two distinct sessions for the same driver had
identical created_at down to microseconds (essentially never
in practice), this would still work — they're both the same
driver's. Cross-driver collisions on the millisecond would be
required to misattribute, and SessionRow.created_at uses
server_default=func.now() which is microsecond-precise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import delete, select

from infra.orm import (
    CarModel,
    CohortMetric,
    TrackLayout,
)
from infra.orm import (
    Session as SessionRow,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def purge_cohort_metrics_for_driver(
    db: AsyncSession,
    *,
    driver_id: UUID,
) -> int:
    """Delete every cohort_metric row attributable to `driver_id`.

    Returns the count of rows deleted. Driver with no sessions →
    0 deleted (no-op, no error). Sessions whose manifest is
    missing track_id/car_id or whose catalog rows can't be
    resolved are skipped — matches the T055 ingest skip-not-error
    posture.
    """
    sessions = (
        (
            await db.execute(
                select(SessionRow).where(
                    SessionRow.driver_id == driver_id
                )
            )
        )
        .scalars()
        .all()
    )

    deleted_total = 0
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

        result = await db.execute(
            delete(CohortMetric).where(
                CohortMetric.car_id == car.master_car_id,
                CohortMetric.track_id == track.master_track_id,
                CohortMetric.session_time == session.created_at,
            )
        )
        # `rowcount` is on CursorResult (DML); the static type
        # is the more general Result. Runtime is correct.
        deleted_total += result.rowcount or 0  # type: ignore[attr-defined]

    return deleted_total
