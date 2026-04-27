"""T095 (sprint2-backend) — event post-mortem feedback loop.

Pure-domain function. After a TargetEvent has ended, compares
the predicted readiness (DriverReadinessState computed before
the event start) to actual driver behavior in the event window
(sessions logged), producing an EventPostMortem record. The
record is the input to the future learning loop — over many
events the system will calibrate readiness scoring against
"did the driver actually show up and race".

Pinned behaviors:
  - event still in progress (now < event end) → returns None;
    a post-mortem before the window closes is premature
  - event with no ends_at uses starts_at + 12h as the effective
    end (matches the longest typical sim/track-day session)
  - predicted_readiness_score = the readiness state with the
    LATEST computed_at strictly before starts_at (not after —
    a state computed during the event would carry mid-event
    info that the prediction can't have used)
  - no readiness state recorded before the event → predicted
    is None (must not crash the loop; the post-mortem still
    records actual_sessions)
  - actual_sessions_completed counts sessions whose created_at
    is in [starts_at, effective_end] inclusive
  - sessions outside the window (before starts_at or after
    effective_end) are NOT counted (those belong to other
    events / generic practice)
  - status "engaged" when actual ≥ 1, "no_show" when actual = 0
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from domain.event_post_mortem import (
    EventPostMortem,
    compute_event_post_mortem,
)

_DRIVER_ID = uuid4()
_EVENT_ID = uuid4()
_NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class _Event:
    starts_at: datetime
    ends_at: datetime | None = None
    id: UUID = field(default_factory=lambda: _EVENT_ID)
    driver_id: UUID = field(default_factory=lambda: _DRIVER_ID)


@dataclass(frozen=True)
class _Readiness:
    computed_at: datetime
    readiness_score: float


@dataclass(frozen=True)
class _Session:
    created_at: datetime


# -- not yet ended ----------------------------------------------


def test_event_in_progress_returns_none() -> None:
    """Event has not ended yet → no post-mortem. Pin: a regression
    that wrote a post-mortem mid-event would lock in a verdict
    before the driver finished, polluting the learning signal."""
    event = _Event(starts_at=_NOW - timedelta(hours=1))  # ends_at None → +12h
    result = compute_event_post_mortem(
        target_event=event,
        readiness_states=[],
        sessions=[],
        now=_NOW,
    )
    assert result is None


def test_event_with_explicit_ends_at_in_future_returns_none() -> None:
    """Same as above with explicit ends_at. Pin: must respect
    ends_at when set rather than fall back to the +12h heuristic."""
    event = _Event(
        starts_at=_NOW - timedelta(hours=20),
        ends_at=_NOW + timedelta(hours=1),
    )
    assert compute_event_post_mortem(
        target_event=event,
        readiness_states=[],
        sessions=[],
        now=_NOW,
    ) is None


# -- ended, with predicted readiness ----------------------------


def test_predicted_readiness_picks_latest_state_before_event_start(
) -> None:
    """Three readiness states: one too old, one closest to event,
    one DURING the event. Predicted must be the closest-to-start
    state STRICTLY BEFORE start. Pin: a regression that picked
    "latest overall" would include a state recomputed during the
    event itself — invalid as a "prediction"."""
    event = _Event(
        starts_at=_NOW - timedelta(hours=24),  # event ended ~12h ago
    )
    readiness_states = [
        _Readiness(
            computed_at=_NOW - timedelta(days=10),
            readiness_score=20.0,  # too old
        ),
        _Readiness(
            computed_at=_NOW - timedelta(hours=25),
            readiness_score=80.0,  # closest before start ← predicted
        ),
        _Readiness(
            computed_at=_NOW - timedelta(hours=20),
            readiness_score=99.0,  # DURING event — must NOT be picked
        ),
    ]
    result = compute_event_post_mortem(
        target_event=event,
        readiness_states=readiness_states,
        sessions=[],
        now=_NOW,
    )
    assert result is not None
    assert result.predicted_readiness_score == 80.0


def test_no_readiness_state_before_event_returns_predicted_none(
) -> None:
    """Driver pinned an event but never had readiness recomputed
    before it started. The post-mortem still emits, but with
    predicted=None. Pin: a missing prediction must not crash —
    the actual side of the comparison is still meaningful."""
    event = _Event(starts_at=_NOW - timedelta(hours=24))
    result = compute_event_post_mortem(
        target_event=event,
        readiness_states=[],
        sessions=[],
        now=_NOW,
    )
    assert result is not None
    assert result.predicted_readiness_score is None


# -- actual sessions counting -----------------------------------


def test_sessions_in_window_are_counted(
) -> None:
    """Two sessions within the event window, one before, one
    after. actual_sessions_completed must be 2. Pin: an off-by-
    one regression on either edge would silently miscount."""
    event = _Event(
        starts_at=_NOW - timedelta(hours=24),
        ends_at=_NOW - timedelta(hours=18),
    )
    sessions = [
        _Session(created_at=_NOW - timedelta(hours=25)),  # before
        _Session(created_at=_NOW - timedelta(hours=23)),  # in
        _Session(created_at=_NOW - timedelta(hours=20)),  # in
        _Session(created_at=_NOW - timedelta(hours=10)),  # after
    ]
    result = compute_event_post_mortem(
        target_event=event,
        readiness_states=[],
        sessions=sessions,
        now=_NOW,
    )
    assert result is not None
    assert result.actual_sessions_completed == 2


def test_no_sessions_yields_no_show_status(
) -> None:
    """Driver pinned an event, didn't show up. status="no_show".
    Pin: the headline learning signal — high readiness + no_show
    means the readiness score is over-counting something."""
    event = _Event(starts_at=_NOW - timedelta(hours=24))
    result = compute_event_post_mortem(
        target_event=event,
        readiness_states=[],
        sessions=[],
        now=_NOW,
    )
    assert result is not None
    assert result.actual_sessions_completed == 0
    assert result.engagement_status == "no_show"


def test_one_or_more_sessions_yields_engaged_status(
) -> None:
    """At least one session in the event window → engaged. Pin:
    distinct from no_show so the learning loop can split
    "showed up at all" from "ghosted entirely"."""
    event = _Event(
        starts_at=_NOW - timedelta(hours=24),
        ends_at=_NOW - timedelta(hours=20),
    )
    result = compute_event_post_mortem(
        target_event=event,
        readiness_states=[],
        sessions=[_Session(created_at=_NOW - timedelta(hours=22))],
        now=_NOW,
    )
    assert result is not None
    assert result.engagement_status == "engaged"


# -- ends_at fallback heuristic ---------------------------------


def test_event_without_ends_at_uses_starts_at_plus_12h(
) -> None:
    """ends_at None → effective end = starts_at + 12h. A session
    11h after start should still count; one 13h after must not.
    Pin: the +12h heuristic covers a single endurance race
    safely; widening it would absorb practice sessions belonging
    to the next event."""
    starts = _NOW - timedelta(hours=15)  # event ended ~3h ago in the +12h model
    event = _Event(starts_at=starts, ends_at=None)
    sessions = [
        _Session(created_at=starts + timedelta(hours=11)),  # in window
        _Session(created_at=starts + timedelta(hours=13)),  # outside +12h
    ]
    result = compute_event_post_mortem(
        target_event=event,
        readiness_states=[],
        sessions=sessions,
        now=_NOW,
    )
    assert result is not None
    assert result.actual_sessions_completed == 1


# -- structural typing -----------------------------------------


def test_record_carries_event_and_driver_ids_for_persistence(
) -> None:
    """The persistence layer keys post-mortems by event+driver.
    Pin: omitting either id would force the caller to re-thread
    them through the call site."""
    event = _Event(starts_at=_NOW - timedelta(hours=24))
    result = compute_event_post_mortem(
        target_event=event,
        readiness_states=[],
        sessions=[],
        now=_NOW,
    )
    assert result is not None
    assert isinstance(result, EventPostMortem)
    assert result.target_event_id == event.id
    assert result.driver_id == event.driver_id
    assert result.computed_at == _NOW
