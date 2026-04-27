"""T092 (sprint2-backend) — event suggestion algorithm.

Pure-domain ranker. Given upcoming calendar candidates and the
driver's race history, returns the candidates ordered by
familiarity match. No database access — the caller hydrates
both sides and feeds them in.

Score model (additive, integer):
  - track_match: +2  — knowing the track is the strongest
    practice-readiness signal (line, braking points, sector splits)
  - car_class_match: +1 — handling transfers across tracks but
    track-specific knowledge has to be relearned

A candidate with score 0 is dropped: returning random events for
a driver with no overlap creates noise the UI can't filter. We'd
rather show "no recommendations yet" than "here are 10 events
you have no relationship with".

Tiebreak: equal scores resolve by `starts_at` ascending. Closer
events have less prep runway, so a recommendation there has more
leverage than the same recommendation 6 months out.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

_TRACK_WEIGHT = 2
_CAR_CLASS_WEIGHT = 1


@dataclass(frozen=True)
class CalendarCandidate:
    """The slice of IRacingCalendarEntry the algorithm needs.

    Kept as a free-text shape (track_name str, car_class str) to
    match what the scraper persists; resolution to the catalog
    (TrackLayout / CarModel) is a downstream concern.
    """

    source_id: str
    event_name: str
    track_name: str | None
    car_class: str | None
    starts_at: datetime


@dataclass(frozen=True)
class DriverHistory:
    """Aggregate of what the driver has raced. Free-text sets
    (not catalog UUIDs) so they line up with the scraper's
    free-text candidate fields. The hydrator (caller) projects
    Session.manifest history into these sets."""

    track_names: frozenset[str]
    car_classes: frozenset[str]


@dataclass(frozen=True)
class EventSuggestion:
    """Recommendation pair: the calendar candidate plus its
    score. The candidate is held by reference so the UI can
    render event_name / starts_at without a second lookup."""

    candidate: CalendarCandidate
    score: int


def _score(candidate: CalendarCandidate, history: DriverHistory) -> int:
    total = 0
    if (
        candidate.track_name is not None
        and candidate.track_name in history.track_names
    ):
        total += _TRACK_WEIGHT
    if (
        candidate.car_class is not None
        and candidate.car_class in history.car_classes
    ):
        total += _CAR_CLASS_WEIGHT
    return total


def suggest_events(
    *,
    candidates: Iterable[CalendarCandidate],
    history: DriverHistory,
    limit: int = 10,
) -> list[EventSuggestion]:
    """Rank `candidates` by familiarity match against `history`.

    Returns at most `limit` suggestions, ordered by score DESC
    then `starts_at` ASC. Candidates with score 0 are excluded.
    """
    scored = [
        EventSuggestion(candidate=c, score=_score(c, history))
        for c in candidates
    ]
    scored = [s for s in scored if s.score > 0]
    scored.sort(
        key=lambda s: (-s.score, s.candidate.starts_at)
    )
    return scored[:limit]
