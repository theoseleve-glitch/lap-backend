"""T092 (sprint2-backend) — event suggestion algorithm.

Pure-domain function. Given a list of upcoming
IRacingCalendarEntry-shaped candidates and the driver's
free-text history (track_names raced, car_classes raced),
return the candidates ranked by familiarity match.

Why pure-domain (no DB): the upstream sources
(IRacingCalendarEntry rows, Session.manifest tracks/cars) are
fed in by the caller. Keeping the algorithm separate means we
test scoring in isolation, and the (future) endpoint orchestrator
test focuses on wiring instead of re-asserting scoring rules.

Score model:
  - track_match: +2 (driver knows the track → highest leverage)
  - car_class_match: +1 (handling transfers across tracks)
  - 0 → no recommendation; the algorithm filters those out so
    callers don't show "irrelevant" suggestions

Tiebreak on equal score: sooner `starts_at` wins (the upcoming
calendar is sorted earliest-first; closer events have less prep
runway and more value as a recommendation).

Pinned behaviors:
  - empty history → empty suggestions (sprint-only driver who
    has never raced; we don't manufacture recommendations from
    thin air)
  - track-only match → ranked above pure stranger events
  - car-class-only match → ranked above pure stranger events
  - both-match → highest score, ahead of single-axis matches
  - ties broken by starts_at ascending (sooner first)
  - limit caps the output length
  - candidates with track_name=None and car_class=None never
    score (unknown event metadata can't match anything)
"""

from __future__ import annotations

from datetime import UTC, datetime

from domain.event_suggestion import (
    CalendarCandidate,
    DriverHistory,
    EventSuggestion,
    suggest_events,
)


def _candidate(
    *,
    source_id: str,
    starts_at: datetime,
    track_name: str | None = None,
    car_class: str | None = None,
    event_name: str = "Round 1",
) -> CalendarCandidate:
    return CalendarCandidate(
        source_id=source_id,
        event_name=event_name,
        track_name=track_name,
        car_class=car_class,
        starts_at=starts_at,
    )


_FUTURE = datetime(2026, 5, 1, tzinfo=UTC)


# -- empty / sentinel -------------------------------------------


def test_empty_history_yields_no_suggestions() -> None:
    """Sprint-only driver with no race history. We don't have a
    signal to recommend on; returning random events would be
    worse than returning nothing. Pin: a regression that scored
    every candidate at 0.5 (e.g., a default constant) would
    surface random noise to the UI."""
    suggestions = suggest_events(
        candidates=[
            _candidate(
                source_id="x",
                starts_at=_FUTURE,
                track_name="Spa",
                car_class="GT3",
            )
        ],
        history=DriverHistory(
            track_names=frozenset(),
            car_classes=frozenset(),
        ),
    )
    assert suggestions == []


def test_unknown_metadata_candidate_never_scores() -> None:
    """A calendar entry with track_name=None AND car_class=None
    cannot match anything, even for an experienced driver. Pin:
    a regression that defaulted None to a magic string that
    accidentally matched would show "your familiar event" cards
    pointing at events with no metadata."""
    suggestions = suggest_events(
        candidates=[
            _candidate(
                source_id="bare", starts_at=_FUTURE,
                track_name=None, car_class=None,
            ),
        ],
        history=DriverHistory(
            track_names=frozenset({"Spa"}),
            car_classes=frozenset({"GT3"}),
        ),
    )
    assert suggestions == []


# -- single-axis match ------------------------------------------


def test_track_match_alone_produces_a_suggestion() -> None:
    """The driver has raced Spa before; Spa shows up in the
    upcoming calendar. That's a recommendation. Pin: dropping
    track scoring would silence the most common path (a returning
    driver who's only run a few tracks)."""
    suggestions = suggest_events(
        candidates=[
            _candidate(
                source_id="spa-race", starts_at=_FUTURE,
                track_name="Spa", car_class="LMP2",
            ),
        ],
        history=DriverHistory(
            track_names=frozenset({"Spa"}),
            car_classes=frozenset({"GT3"}),
        ),
    )
    assert len(suggestions) == 1
    assert suggestions[0].candidate.source_id == "spa-race"
    assert suggestions[0].score == 2


def test_car_class_match_alone_produces_a_suggestion() -> None:
    """Driver hasn't raced this track but has experience with
    the car class. Still a useful recommendation — handling
    transfers across venues. Pin: dropping the car axis would
    silence cross-venue practice opportunities."""
    suggestions = suggest_events(
        candidates=[
            _candidate(
                source_id="new-track-known-car",
                starts_at=_FUTURE,
                track_name="Bathurst",
                car_class="GT3",
            ),
        ],
        history=DriverHistory(
            track_names=frozenset({"Spa"}),
            car_classes=frozenset({"GT3"}),
        ),
    )
    assert len(suggestions) == 1
    assert suggestions[0].score == 1


# -- ranking ----------------------------------------------------


def test_both_match_scores_higher_than_single_axis() -> None:
    """Track AND car familiarity together is the strongest
    recommendation — least new ground for the driver to cover.
    Pin: equal weights would tie the dual-match with a track-only
    event, losing the "best practice fit" signal."""
    suggestions = suggest_events(
        candidates=[
            _candidate(
                source_id="track-only",
                starts_at=_FUTURE,
                track_name="Spa",
                car_class="LMP2",  # not in history
            ),
            _candidate(
                source_id="dual-match",
                starts_at=_FUTURE,
                track_name="Spa",
                car_class="GT3",
            ),
        ],
        history=DriverHistory(
            track_names=frozenset({"Spa"}),
            car_classes=frozenset({"GT3"}),
        ),
    )
    assert [s.candidate.source_id for s in suggestions] == [
        "dual-match", "track-only",
    ]


def test_tie_on_score_breaks_by_earlier_starts_at() -> None:
    """Two events both score the same? Take the sooner one —
    less prep runway means higher-value recommendation. Pin: a
    regression that broke ties by source_id alphabetically would
    silently surface "later" events. The source_ids are chosen
    so that the alphabetic order is the OPPOSITE of the time
    order — the test only passes if ties truly break by time."""
    earlier = datetime(2026, 5, 1, tzinfo=UTC)
    later = datetime(2026, 7, 1, tzinfo=UTC)
    suggestions = suggest_events(
        candidates=[
            _candidate(
                source_id="a-but-later", starts_at=later,
                track_name="Spa", car_class="GT3",
            ),
            _candidate(
                source_id="z-but-earlier", starts_at=earlier,
                track_name="Spa", car_class="GT3",
            ),
        ],
        history=DriverHistory(
            track_names=frozenset({"Spa"}),
            car_classes=frozenset({"GT3"}),
        ),
    )
    assert [s.candidate.source_id for s in suggestions] == [
        "z-but-earlier", "a-but-later",
    ]


# -- limit ------------------------------------------------------


def test_limit_caps_result_count() -> None:
    """With many strong matches, the UI doesn't want a flood.
    Pin: passing limit=2 must return exactly 2 even when 4
    candidates would qualify."""
    candidates = [
        _candidate(
            source_id=f"r{i}",
            starts_at=datetime(2026, 5, i + 1, tzinfo=UTC),
            track_name="Spa",
            car_class="GT3",
        )
        for i in range(4)
    ]
    suggestions = suggest_events(
        candidates=candidates,
        history=DriverHistory(
            track_names=frozenset({"Spa"}),
            car_classes=frozenset({"GT3"}),
        ),
        limit=2,
    )
    assert len(suggestions) == 2


# -- structural typing -------------------------------------------


def test_suggestion_carries_full_candidate_for_ui() -> None:
    """Caller (UI orchestrator) needs the original candidate
    fields (event_name, starts_at) to render the recommendation
    card. Pin: returning bare source_ids would force a second
    round-trip through the caller. Returning the candidate by
    reference keeps the algorithm caller-friendly."""
    cand = _candidate(
        source_id="x",
        starts_at=_FUTURE,
        track_name="Spa",
        car_class="GT3",
        event_name="Spa 6h Special",
    )
    [suggestion] = suggest_events(
        candidates=[cand],
        history=DriverHistory(
            track_names=frozenset({"Spa"}),
            car_classes=frozenset(),
        ),
    )
    assert isinstance(suggestion, EventSuggestion)
    assert suggestion.candidate is cand
    assert suggestion.candidate.event_name == "Spa 6h Special"
