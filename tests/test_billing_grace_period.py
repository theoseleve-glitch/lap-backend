"""T163 — `is_in_grace_period` is a pure helper for the trial-expired
soft-block window. Stripe's billing lifecycle rolls a trial into one
of `active` (paid), `past_due` (card failed), `canceled` (no payment
method), or `unpaid` (final dunning state). For 48 hours after the
trial winds down to a non-active state, we let the user keep using
the app behind a banner so they have time to fix their card."""

from datetime import UTC, datetime, timedelta

from domain.billing import GRACE_PERIOD_DURATION, is_in_grace_period


def test_grace_period_duration_is_48_hours() -> None:
    assert timedelta(hours=48) == GRACE_PERIOD_DURATION


def test_active_user_is_not_in_grace_period() -> None:
    now = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
    assert is_in_grace_period("active", None, now) is False


def test_trialing_user_is_not_in_grace_period() -> None:
    # Still trialing — they have full access via the strict gate, no
    # need for the grace fallback.
    now = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
    assert (
        is_in_grace_period("trialing", now - timedelta(hours=1), now)
        is False
    )


def test_past_due_within_window_is_in_grace_period() -> None:
    now = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
    ended = now - timedelta(hours=24)
    assert is_in_grace_period("past_due", ended, now) is True


def test_past_due_just_inside_48h_boundary_is_in_grace_period() -> None:
    now = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
    ended = now - timedelta(hours=47, minutes=59)
    assert is_in_grace_period("past_due", ended, now) is True


def test_past_due_at_48h_boundary_is_no_longer_in_grace_period() -> None:
    # Exactly 48h after trial end → grace expired, hard block kicks in.
    now = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
    ended = now - timedelta(hours=48)
    assert is_in_grace_period("past_due", ended, now) is False


def test_past_due_beyond_48h_is_not_in_grace_period() -> None:
    now = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
    ended = now - timedelta(hours=72)
    assert is_in_grace_period("past_due", ended, now) is False


def test_canceled_within_window_is_in_grace_period() -> None:
    # Trial ran out and user never added a card → status=canceled,
    # but they get the same 48h grace as past_due.
    now = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
    ended = now - timedelta(hours=10)
    assert is_in_grace_period("canceled", ended, now) is True


def test_unpaid_within_window_is_in_grace_period() -> None:
    now = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
    ended = now - timedelta(hours=10)
    assert is_in_grace_period("unpaid", ended, now) is True


def test_inactive_with_no_trial_marker_is_not_in_grace_period() -> None:
    # Default state — never subscribed. No grace, no trial.
    now = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
    assert is_in_grace_period("inactive", None, now) is False


def test_past_due_without_trial_marker_is_not_in_grace_period() -> None:
    # Lapsed paid customer (active → past_due via failed renewal,
    # never had a trial). Standard Stripe dunning applies but no
    # soft-block window.
    now = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
    assert is_in_grace_period("past_due", None, now) is False


def test_active_with_trial_marker_is_not_in_grace_period() -> None:
    # User who completed a trial and converted to paid — trial_ended_at
    # is set historically, but they don't need grace.
    now = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
    ended = now - timedelta(hours=1)
    assert is_in_grace_period("active", ended, now) is False


def test_naive_trial_ended_at_is_treated_as_utc() -> None:
    # SQLite stores DateTime(tz=True) as naive; on read-back the value
    # comes off without tzinfo. The helper normalizes to UTC so callers
    # don't have to.
    now = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
    naive_ended = datetime(2026, 4, 25, 11, 0)  # 1h ago, naive
    assert is_in_grace_period("past_due", naive_ended, now) is True
