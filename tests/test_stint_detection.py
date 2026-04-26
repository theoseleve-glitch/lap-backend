"""T068 (sprint2-backend) — stint detection from pit-event markers.

Pure-domain function that splits a long session's lap range into
stints, given the laps where the driver pitted in.

Why a pure function (not reading Lap rows directly): the codebase
doesn't yet have a pit-event entity (T097 introduces StrategyEvent
with kind="pit", at_lap). T068's caller decides what counts as a
pit (telemetry channel, sim-emitted event, manual log) and just
hands us the resulting lap numbers. This keeps the detector
forward-compatible with whatever pit-source lands in T097.

Semantics (pinned by the tests below):
  - A pit at lap N closes the current stint at N (the in-lap is
    the last lap of that stint).
  - The next stint begins at N+1 unless N == total_laps (final-lap
    pit just ends the session).
  - No pits → one stint covering all laps.
  - total_laps == 0 → no stints (empty session).

Invalid inputs raise ValueError up-front (pit < 1, pit > total_laps,
duplicate or unsorted pit numbers). We refuse rather than silently
returning malformed stints — a downstream "the driver did 4 stints
that all overlap" bug would be much harder to debug.
"""

from __future__ import annotations

import pytest

from domain.stint_detection import Stint, detect_stints

# -- happy paths --------------------------------------------------


def test_no_pits_produces_one_stint_covering_all_laps() -> None:
    """A pit-free session is one continuous stint. Pin the
    no-pit base case — without it, detect_stints would have to
    encode "1 stint" specially in every caller."""
    stints = detect_stints(total_laps=15, pit_in_laps=[])
    assert stints == [Stint(start_lap=1, end_lap=15, lap_count=15)]


def test_single_mid_session_pit_splits_into_two_stints() -> None:
    """Pit at lap 12 in a 25-lap run: stint 1 = laps 1-12 (in-lap
    counts as part of the closing stint), stint 2 = laps 13-25.
    Pin the boundary convention — off-by-one here would assign
    the in-lap to the next stint and skew per-stint pace metrics."""
    stints = detect_stints(total_laps=25, pit_in_laps=[12])
    assert stints == [
        Stint(start_lap=1, end_lap=12, lap_count=12),
        Stint(start_lap=13, end_lap=25, lap_count=13),
    ]


def test_multiple_pits_produce_n_plus_one_stints() -> None:
    """Three pits at laps 10, 22, 38 in a 50-lap run produce four
    stints with contiguous, non-overlapping boundaries. Pin the
    iteration: one extra stint at the end after all pits."""
    stints = detect_stints(total_laps=50, pit_in_laps=[10, 22, 38])
    assert stints == [
        Stint(start_lap=1, end_lap=10, lap_count=10),
        Stint(start_lap=11, end_lap=22, lap_count=12),
        Stint(start_lap=23, end_lap=38, lap_count=16),
        Stint(start_lap=39, end_lap=50, lap_count=12),
    ]


def test_pit_on_final_lap_does_not_create_empty_trailing_stint() -> None:
    """If the driver pits at the very last lap (e.g., end of race
    pit-in for parc fermé), do NOT emit an empty stint after.
    Pin the boundary: a stint of length 0 is meaningless and
    would crash any downstream pace-decay calculation."""
    stints = detect_stints(total_laps=20, pit_in_laps=[8, 20])
    assert stints == [
        Stint(start_lap=1, end_lap=8, lap_count=8),
        Stint(start_lap=9, end_lap=20, lap_count=12),
    ]


def test_zero_lap_session_produces_no_stints() -> None:
    """A session with no completed laps (e.g., logged-out before
    crossing the line) yields an empty list, not a length-0 stint.
    Pin the no-laps base case; downstream consumers iterate."""
    assert detect_stints(total_laps=0, pit_in_laps=[]) == []


# -- invalid inputs -----------------------------------------------


def test_pit_lap_below_one_raises() -> None:
    """Lap numbers are 1-indexed in this codebase (see Lap.lap_number).
    A 0 or negative pit lap is a caller bug — raise rather than
    silently mangling the stint boundaries."""
    with pytest.raises(ValueError, match="pit_in_laps"):
        detect_stints(total_laps=10, pit_in_laps=[0])


def test_pit_lap_above_total_laps_raises() -> None:
    """A pit beyond the recorded lap count means the lap data is
    out of sync with the pit data. Refuse — this is exactly the
    case where silently producing a malformed Stint would corrupt
    long-run metrics for the rest of the pipeline."""
    with pytest.raises(ValueError, match="pit_in_laps"):
        detect_stints(total_laps=10, pit_in_laps=[11])


def test_unsorted_or_duplicate_pit_laps_raises() -> None:
    """The function takes the pit list as-is — sorting it for the
    caller hides bugs (the same lap number twice usually means a
    duplicate event got logged). Refuse and let the caller dedup
    + sort consciously."""
    with pytest.raises(ValueError, match="pit_in_laps"):
        detect_stints(total_laps=20, pit_in_laps=[10, 5])
    with pytest.raises(ValueError, match="pit_in_laps"):
        detect_stints(total_laps=20, pit_in_laps=[10, 10])
