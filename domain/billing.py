"""Billing domain logic — pure helpers, no I/O.

T163 introduced the trial-end grace period. When a Stripe trial winds
down to a non-active status (`past_due`, `canceled`, `unpaid`), give
the user 48 hours of soft access so they can fix their card before
the hard 402 block kicks in.
"""

from datetime import UTC, datetime, timedelta

GRACE_PERIOD_DURATION = timedelta(hours=48)

# Statuses that *can* trigger grace. `inactive` (default — never
# subscribed) is excluded; standard `active`/`trialing` users don't
# need grace because the strict gate already lets them through.
_STATUSES_ELIGIBLE_FOR_GRACE = frozenset({"past_due", "canceled", "unpaid"})


def is_in_grace_period(
    subscription_status: str,
    trial_ended_at: datetime | None,
    now: datetime,
) -> bool:
    """Return True if a user whose trial ended <48h ago should still
    have access. Both arguments are tolerated as naive (assumed UTC)
    so callers don't need to normalize SQLite reads."""
    if trial_ended_at is None:
        return False
    if subscription_status not in _STATUSES_ELIGIBLE_FOR_GRACE:
        return False
    if trial_ended_at.tzinfo is None:
        trial_ended_at = trial_ended_at.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return (now - trial_ended_at) < GRACE_PERIOD_DURATION
