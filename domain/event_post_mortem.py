"""T095 (sprint2-backend) — event post-mortem feedback loop.

Pure-domain function. After a TargetEvent has ended, builds an
EventPostMortem comparing predicted readiness (the readiness
state the system held just before the event started) to actual
driver behavior in the event window (sessions logged).

The record is the input to the future learning loop: over many
events, the system can correlate "we predicted score X" with
"the driver did Y", and recalibrate the readiness components
that consistently miss.

Scope: this module emits the record only. Persistence and the
Celery sweep that schedules computation per ended event are
follow-up tasks — keeping the math pure-domain means the
learning loop's input contract is settled before persistence.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

# Default event-window length when ends_at is None. 12h covers
# a typical sim endurance race / track day; longer would absorb
# practice sessions from the next pinned event.
_DEFAULT_WINDOW = timedelta(hours=12)


class _EventLike(Protocol):
    id: UUID
    driver_id: UUID
    starts_at: datetime
    ends_at: datetime | None


class _ReadinessLike(Protocol):
    computed_at: datetime
    readiness_score: float


class _SessionLike(Protocol):
    created_at: datetime


@dataclass(frozen=True)
class EventPostMortem:
    """One post-event verdict. Persisted (in a future task)
    keyed by (target_event_id, driver_id).

    `predicted_readiness_score` is None when the system never
    computed a readiness state before the event started — the
    actual side of the comparison still lands so the loop has
    SOMETHING to record."""

    target_event_id: UUID
    driver_id: UUID
    computed_at: datetime
    predicted_readiness_score: float | None
    actual_sessions_completed: int
    engagement_status: str  # "engaged" | "no_show"


def _effective_end(event: _EventLike) -> datetime:
    return event.ends_at if event.ends_at is not None else (
        event.starts_at + _DEFAULT_WINDOW
    )


def compute_event_post_mortem(
    *,
    target_event: _EventLike,
    readiness_states: Iterable[_ReadinessLike],
    sessions: Iterable[_SessionLike],
    now: datetime,
) -> EventPostMortem | None:
    """Build a post-mortem for `target_event`. Returns None
    when the event hasn't ended yet — a verdict before the
    window closes is premature.

    `predicted` picks the readiness state with the latest
    computed_at strictly before `target_event.starts_at`.
    A state computed during the event would carry mid-event
    info that the prediction by definition couldn't have used.
    """
    end = _effective_end(target_event)
    if now < end:
        return None

    predicted: float | None = None
    latest_before: datetime | None = None
    for r in readiness_states:
        if r.computed_at < target_event.starts_at and (
            latest_before is None or r.computed_at > latest_before
        ):
            latest_before = r.computed_at
            predicted = r.readiness_score

    actual = sum(
        1 for s in sessions
        if target_event.starts_at <= s.created_at <= end
    )
    status = "engaged" if actual >= 1 else "no_show"

    return EventPostMortem(
        target_event_id=target_event.id,
        driver_id=target_event.driver_id,
        computed_at=now,
        predicted_readiness_score=predicted,
        actual_sessions_completed=actual,
        engagement_status=status,
    )
