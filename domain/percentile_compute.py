"""T057 (sprint2-backend) — percentile compute.

Two layers:
  1. compute_percentile_rank — pure math. Midrank convention so
     ties split symmetrically (50/50) instead of one-sidedly.
  2. cohort_percentile_for_value — fetches cohort_metrics for
     the (car, track, metric) triple, applies the T056
     eligibility gate, and returns the driver value's rank
     against that distribution. Returns None when the cohort
     hasn't unlocked yet (callers must handle the null).

Why fetch + filter Python-side rather than SQL aggregate:
  the cohort is already small (per T056 we publish only
  when ≥20 distinct drivers exist; total rows per
  (car,track,metric) typically scales like
  drivers × sessions, on the order of low thousands). A
  Python distribution sort is well within the nightly job's
  latency budget. SQL percentile_cont could be used later
  if scale demands.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from domain.cohort_minimum import is_cohort_eligible_for_percentile
from infra.orm import CohortMetric

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def compute_percentile_rank(
    cohort_values: Sequence[float], value: float
) -> float:
    """Midrank percentile of `value` in `cohort_values`, in [0, 1].

    Midrank: rank = (#strictly_below + 0.5 * #equal) / N.
    Treats ties symmetrically — a value tied with K cohort
    members is placed at the centroid of those K positions.

    Raises ValueError on empty cohort (callers must gate via
    T056 first; an empty cohort is a programming error here).
    """
    n = len(cohort_values)
    if n == 0:
        raise ValueError("cohort_values cannot be empty")
    strictly_below = sum(1 for v in cohort_values if v < value)
    equal = sum(1 for v in cohort_values if v == value)
    return (strictly_below + 0.5 * equal) / n


async def cohort_percentile_for_value(
    db: AsyncSession,
    *,
    car_id: UUID,
    track_id: UUID,
    metric: str,
    value: float,
) -> float | None:
    """Driver's percentile in the cohort, or None if not eligible.

    Reads cohort_metrics on (car_id, track_id, metric). Returns
    None when the cohort is below MIN_COHORT_SIZE drivers
    (T056) — that's the contract T058 leans on.
    """
    if not await is_cohort_eligible_for_percentile(
        db, car_id=car_id, track_id=track_id
    ):
        return None
    rows = (
        (
            await db.execute(
                select(CohortMetric.value).where(
                    CohortMetric.car_id == car_id,
                    CohortMetric.track_id == track_id,
                    CohortMetric.metric == metric,
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    return compute_percentile_rank(list(rows), value)
