"""T052 (sprint2-backend) — cohort privacy enforcement.

Cohort benchmark calculations (T054+) pool aggregate telemetry
across drivers. Only drivers who have explicitly opted in
(`driver_profiles.cohort_opt_in = TRUE`) are eligible to
contribute. Default for new accounts is FALSE — opt-in is the
consent model, not opt-out, since cohort calculations expose
shape/intensity stats derived from a driver's sessions.

Two enforcement entry points:
  - `is_eligible_for_cohort(bool)`: pure predicate for in-memory
    filtering (e.g., already-loaded ORM rows)
  - `cohort_eligible_drivers_filter()`: SQLAlchemy expression for
    SELECT clauses

Both must agree on classification — pinned by a test that runs
the same fixture through both.

The SQL filter helper is in this domain module (not infra/) so
the privacy rule is co-located with the predicate it must match.
A future move to a richer `CohortEligibility` policy lives here
too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.sql.elements import ColumnElement


def is_eligible_for_cohort(cohort_opt_in: bool) -> bool:
    """Pure predicate: True iff the driver has opted in. Default
    (False) is non-consent."""
    return cohort_opt_in


def cohort_eligible_drivers_filter() -> ColumnElement[bool]:
    """SQLAlchemy filter for SELECTs that should only see opt-in
    drivers. Use as `where(cohort_eligible_drivers_filter())` on
    any cohort-data-consuming query."""
    # Imported lazily so domain module doesn't take an infra
    # dependency at import time — matches the pure-domain rule.
    from infra.orm import DriverProfile

    return DriverProfile.cohort_opt_in.is_(True)
