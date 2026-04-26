"""T223 — Prescription expiry sweep (calendar-driven termination).

Pure DB function: scans `action_prescriptions` for actionable
rows whose `valid_until` is in the past and flips them to
'expired'. The Celery beat hook in `infra.celery_app` runs this
nightly so the user's "open" list doesn't accumulate zombie
rows past their actionable window.

Idempotent — terminal statuses (completed, skipped, expired)
are excluded from the sweep, and re-running on a fresh-expired
row is a no-op for the next pass.

Returns the number of rows actually flipped, so the cron handler
can log a useful "swept N rows" line on busy nights vs. quiet ones.
"""

from datetime import datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from infra.orm import ActionPrescription

# The only statuses that can legitimately become 'expired'.
# Mirrors T217's _OPEN_STATUSES — terminal states are skipped.
_ACTIONABLE_STATUSES = ("proposed", "accepted", "in_progress")
_EXPIRED_STATUS = "expired"


async def expire_overdue_prescriptions(
    db: AsyncSession, *, now: datetime
) -> int:
    """Flip every actionable prescription with `valid_until < now`
    to status='expired'. Returns the count of rows updated."""
    result = await db.execute(
        update(ActionPrescription)
        .where(ActionPrescription.valid_until.is_not(None))
        .where(ActionPrescription.valid_until < now)
        .where(ActionPrescription.status.in_(_ACTIONABLE_STATUSES))
        .values(status=_EXPIRED_STATUS)
    )
    await db.commit()
    # `update()` always returns a CursorResult at runtime, but the
    # async session's execute() is typed as Result[Any]. rowcount
    # is the row count for the UPDATE.
    return int(getattr(result, "rowcount", 0) or 0)
