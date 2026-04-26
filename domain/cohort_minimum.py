"""T056 (sprint2-backend) — cohort minimum (k=20).

Per ADR-008: a percentile is published only when ≥20 distinct
opted-in drivers share the (car, track) context. This is k=20
anonymity at the DRIVER level. A single driver with many
sessions still contributes 1 to the count.

Why this lives outside cohort_metrics:
  cohort_metrics has no driver_id (T054 schema invariant) so we
  can't COUNT(DISTINCT driver_id) on it. The threshold check
  reads from `sessions + driver_profiles + track_layouts +
  car_models` — that path can see driver_id, but emits only an
  aggregate count, which is non-identifying. cohort_metrics
  stays anonymized for the percentile-compute path (T057).

The matching is on MASTER IDs (master_car_id / master_track_id),
matching cohort_metrics' storage. So Spa-iRacing and Spa-AC for
the same car class pool into one cohort.

Implementation note: the (track_id, car_id) live inside the
JSON manifest column, and JSON unwrapping for SQL-side
WHERE clauses is database-specific (SQLite json_extract
returns quoted strings; Postgres uses ->> or .astext). For
portability across the test SQLite and prod Postgres, we
fetch the opt-in sessions and resolve master IDs Python-side.
The dataset is small (only opt-in sessions) so this is
acceptable; a future T can switch to dialect-specific JSON
SQL if cohort scale demands it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from infra.orm import CarModel, DriverProfile, TrackLayout
from infra.orm import Session as SessionRow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

MIN_COHORT_SIZE = 20


async def count_cohort_members(
    db: AsyncSession,
    *,
    car_id: UUID,
    track_id: UUID,
) -> int:
    """Count distinct opted-in drivers with at least one session
    on the (master_car_id, master_track_id) tuple."""
    # 1. Build catalog id → master_id maps once.
    track_master = {
        t.id: t.master_track_id
        for t in (
            await db.execute(select(TrackLayout))
        ).scalars()
    }
    car_master = {
        c.id: c.master_car_id
        for c in (
            await db.execute(select(CarModel))
        ).scalars()
    }
    # 2. Fetch opt-in sessions (driver_id + manifest only).
    rows = (
        await db.execute(
            select(SessionRow.driver_id, SessionRow.manifest)
            .join(
                DriverProfile, DriverProfile.id == SessionRow.driver_id
            )
            .where(DriverProfile.cohort_opt_in.is_(True))
        )
    ).all()
    # 3. Filter Python-side. Distinct driver count is the answer.
    contributors: set[UUID] = set()
    for driver_id, manifest in rows:
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
    return len(contributors)


async def is_cohort_eligible_for_percentile(
    db: AsyncSession,
    *,
    car_id: UUID,
    track_id: UUID,
) -> bool:
    """True iff `count_cohort_members(...) >= MIN_COHORT_SIZE`.

    The chokepoint that gates publish-or-don't on percentiles
    (T058 reads this). Centralizing the check here means a
    future move from k=20 to a different policy is one constant
    change — and any code path that calls this function inherits
    the new policy automatically.
    """
    n = await count_cohort_members(db, car_id=car_id, track_id=track_id)
    return n >= MIN_COHORT_SIZE
