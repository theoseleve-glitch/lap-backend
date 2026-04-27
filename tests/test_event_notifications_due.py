"""T094 (sprint2-backend) — event notification windowing.

Pure-domain function. Given upcoming TargetEvent-shaped candidates
and `now`, returns which (event, window) pairs need an email sent
right now. The Celery task (T094 wiring, separate test) calls
this every hour at minute 0 and dispatches one email per due
notification.

Two windows: "24h" (event ~24h out) and "1h" (event ~1h out).
Each window has a ±30-minute tolerance so a single hourly beat
fire reliably catches each event exactly once.

Idempotency lives on the candidate: `notifications_sent` is a
list of window labels already dispatched. The function filters
on this so a missed cron + recovery re-fire doesn't double-send,
and a clock-skew "almost still in the window" tick doesn't
re-fire either.

Pinned behaviors:
  - empty candidates → empty result (no events, no work)
  - event 24h out + nothing sent → "24h" due
  - event 24h out + "24h" already sent → silent (idempotency)
  - event 1h out + "1h" already sent → silent
  - event 1h out + only "24h" sent → "1h" due (windows independent)
  - event in past → silent (notifications are forward-looking)
  - event 12h out → silent (between windows, no notification)
  - event >24.5h out → silent (outside window upper bound)
  - event <30m out → silent (between final window and start)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from domain.event_notifications import (
    DueNotification,
    compute_due_notifications,
)

_NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class _FakeEvent:
    """Lightweight stand-in matching the duck-typed shape the
    function reads. Keeps the test out of the SQLAlchemy ORM /
    DB session machinery — pure-domain testing."""

    starts_at: datetime
    id: UUID = field(default_factory=uuid4)
    driver_id: UUID = field(default_factory=uuid4)
    notifications_sent: tuple[str, ...] = ()


# -- baseline ----------------------------------------------------


def test_no_candidates_yields_no_due() -> None:
    """No events to scan → no work. Pin: a regression that
    returned a sentinel "you have nothing scheduled" notification
    would spam every driver every hour."""
    assert compute_due_notifications(candidates=[], now=_NOW) == []


# -- 24h window --------------------------------------------------


def test_event_exactly_24h_out_triggers_24h_window() -> None:
    """Boundary firing: when the event is 24h ahead, the 24h
    notification must fire. Pin: this is the headline acceptance
    case — without it the 24h reminder ships nothing."""
    ev = _FakeEvent(starts_at=_NOW + timedelta(hours=24))
    due = compute_due_notifications(candidates=[ev], now=_NOW)
    assert [d.window for d in due] == ["24h"]
    assert due[0].target_event_id == ev.id
    assert due[0].driver_id == ev.driver_id


def test_event_24h_out_skipped_when_already_sent() -> None:
    """Idempotency: if the 24h reminder is in `notifications_sent`,
    don't re-fire. Pin: an hourly cron without idempotency would
    blast 60 emails over an hour as the delta rounds within the
    tolerance window."""
    ev = _FakeEvent(
        starts_at=_NOW + timedelta(hours=24),
        notifications_sent=("24h",),
    )
    assert compute_due_notifications(candidates=[ev], now=_NOW) == []


def test_event_outside_24h_upper_tolerance_silent() -> None:
    """Event 25h out is past the upper edge of the 24h window
    (24h + 30min tolerance). Pin: a regression that widened the
    window would fire 24h reminders multiple times across
    successive hourly beats."""
    ev = _FakeEvent(starts_at=_NOW + timedelta(hours=25, minutes=1))
    assert compute_due_notifications(candidates=[ev], now=_NOW) == []


# -- 1h window ---------------------------------------------------


def test_event_exactly_1h_out_triggers_1h_window() -> None:
    """Boundary firing for the 1h window. Pin: matches the
    24h-window pin one window down — headline acceptance for
    the second reminder."""
    ev = _FakeEvent(starts_at=_NOW + timedelta(hours=1))
    due = compute_due_notifications(candidates=[ev], now=_NOW)
    assert [d.window for d in due] == ["1h"]


def test_event_1h_out_skipped_when_already_sent() -> None:
    """Idempotency for the 1h window."""
    ev = _FakeEvent(
        starts_at=_NOW + timedelta(hours=1),
        notifications_sent=("1h",),
    )
    assert compute_due_notifications(candidates=[ev], now=_NOW) == []


def test_1h_window_independent_of_24h_send() -> None:
    """A driver who got the 24h reminder still needs the 1h
    reminder. Pin: a regression that shared an "already sent"
    flag across windows would silently swallow the 1h reminder
    for every event."""
    ev = _FakeEvent(
        starts_at=_NOW + timedelta(hours=1),
        notifications_sent=("24h",),
    )
    due = compute_due_notifications(candidates=[ev], now=_NOW)
    assert [d.window for d in due] == ["1h"]


# -- silent zones ------------------------------------------------


def test_event_12h_out_no_notification() -> None:
    """Between the windows, there's nothing to send. Pin:
    a regression that fired on every "upcoming event" would
    spam drivers every hour from pin time to event time."""
    ev = _FakeEvent(starts_at=_NOW + timedelta(hours=12))
    assert compute_due_notifications(candidates=[ev], now=_NOW) == []


def test_event_in_past_silent() -> None:
    """The event already happened. Notifications are
    forward-looking. Pin: a "you missed your event" misfire
    hours after the start would be confusing UX."""
    ev = _FakeEvent(starts_at=_NOW - timedelta(hours=1))
    assert compute_due_notifications(candidates=[ev], now=_NOW) == []


def test_event_under_30m_out_silent() -> None:
    """Below the 1h window's lower edge. The driver is already
    racing or pre-grid; another reminder would be noise. Pin:
    a regression that included <30m would bombard the driver
    with reminders during the final approach."""
    ev = _FakeEvent(starts_at=_NOW + timedelta(minutes=20))
    assert compute_due_notifications(candidates=[ev], now=_NOW) == []


# -- multi-event -------------------------------------------------


def test_multiple_events_each_evaluated_independently() -> None:
    """One event in the 24h window, one in the 1h window, one
    out of band. The function returns exactly the two due
    notifications, not three or one. Pin: a regression that
    short-circuited after the first match would only fire one
    of the two due items per hourly beat."""
    far = _FakeEvent(starts_at=_NOW + timedelta(hours=24))
    near = _FakeEvent(starts_at=_NOW + timedelta(hours=1))
    middle = _FakeEvent(starts_at=_NOW + timedelta(hours=10))
    due = compute_due_notifications(
        candidates=[far, near, middle], now=_NOW
    )
    windows = sorted(d.window for d in due)
    assert windows == ["1h", "24h"]


def test_due_notification_carries_driver_id_for_email_lookup() -> None:
    """The dispatcher needs driver_id to look up the email
    address. Pin: returning bare event ids would force the
    caller through a second query per due item."""
    ev = _FakeEvent(starts_at=_NOW + timedelta(hours=24))
    [due] = compute_due_notifications(candidates=[ev], now=_NOW)
    assert isinstance(due, DueNotification)
    assert due.driver_id == ev.driver_id
    assert due.target_event_id == ev.id
