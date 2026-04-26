"""T031 (sprint2-backend) — lap-aligned segmenter.

Given two sessions' sample streams + a corner set, the segmenter
returns paired lap segments: for each (lap_pair_index, corner)
combination, the slice of A's samples and B's samples whose
lap_dist_m falls within the corner's window.

Pairing by lap index (0th distinct lap of A ↔ 0th of B) is the
simplest defensible policy — selecting "fastest lap" or
"comparable lap times" is policy for a higher layer (T034+); the
segmenter just does mechanical slicing.

Corners are track-distance windows `[start_m, end_m]`. They can
overlap; the same sample may appear in multiple corners. The
segmenter doesn't validate geometry.
"""

from __future__ import annotations

from typing import Any

from domain.lap_segmenter import (
    Corner,
    PairedLapSegment,
    segment_paired_laps,
)

# Two corners on a 5km track: T1 at 100..400m, T2 at 2000..2300m.
T1 = Corner(name="T1", start_m=100.0, end_m=400.0)
T2 = Corner(name="T2", start_m=2000.0, end_m=2300.0)


def _s(
    lap: int,
    t_ms: int,
    lap_dist_m: float | None,
    **extra: Any,
) -> dict[str, object]:
    out: dict[str, object] = {
        "t_canonical_ns": t_ms * 1_000_000,
        "lap_number": lap,
    }
    if lap_dist_m is not None:
        out["lap_dist_m"] = lap_dist_m
    out.update(extra)
    return out


# -- segment shape ------------------------------------------------


def test_segments_one_corner_in_one_lap_pair() -> None:
    """Smallest case — one corner, one lap on each side. The
    segment for that (pair, corner) is the contiguous samples in
    the corner's distance window from each session."""
    a = [
        _s(1, 0, 50.0),
        _s(1, 100, 200.0),  # in T1
        _s(1, 200, 350.0),  # in T1
        _s(1, 300, 500.0),
    ]
    b = [
        _s(1, 0, 60.0),
        _s(1, 110, 250.0),  # in T1
        _s(1, 220, 380.0),  # in T1
        _s(1, 330, 490.0),
    ]
    pairs = segment_paired_laps(a, b, corners=(T1,))
    assert len(pairs) == 1
    p = pairs[0]
    assert p.lap_pair_index == 0
    assert p.corner_name == "T1"
    assert [s["lap_dist_m"] for s in p.samples_a] == [200.0, 350.0]
    assert [s["lap_dist_m"] for s in p.samples_b] == [250.0, 380.0]


def test_emits_one_segment_per_corner_per_lap_pair() -> None:
    """Two corners × one lap pair → exactly two segments."""
    a = [_s(1, 0, 200.0), _s(1, 100, 2100.0)]
    b = [_s(1, 0, 250.0), _s(1, 100, 2200.0)]
    pairs = segment_paired_laps(a, b, corners=(T1, T2))
    assert len(pairs) == 2
    by_corner = {p.corner_name: p for p in pairs}
    assert "T1" in by_corner and "T2" in by_corner


# -- lap pairing --------------------------------------------------


def test_pairs_laps_by_index() -> None:
    """Lap 0 of A ↔ lap 0 of B; lap 1 of A ↔ lap 1 of B. The lap
    *number* in the sample stream is incidental — what matters is
    the order in which distinct laps appear."""
    a = [_s(7, 0, 200.0), _s(8, 100, 200.0)]
    b = [_s(3, 0, 200.0), _s(4, 100, 200.0)]
    pairs = segment_paired_laps(a, b, corners=(T1,))
    assert {p.lap_pair_index for p in pairs} == {0, 1}


def test_truncates_to_min_distinct_lap_count() -> None:
    """A has 3 distinct laps, B has 1 → 1 pair total. We never
    pair an A-lap against a missing B-lap (no synthetic empty)."""
    a = [_s(1, 0, 200.0), _s(2, 100, 200.0), _s(3, 200, 200.0)]
    b = [_s(1, 0, 200.0)]
    pairs = segment_paired_laps(a, b, corners=(T1,))
    assert {p.lap_pair_index for p in pairs} == {0}


# -- sample filtering ---------------------------------------------


def test_excludes_samples_outside_corner_window() -> None:
    a = [_s(1, 0, 50.0), _s(1, 100, 200.0), _s(1, 200, 1000.0)]
    b = [_s(1, 0, 60.0), _s(1, 100, 250.0), _s(1, 200, 800.0)]
    pairs = segment_paired_laps(a, b, corners=(T1,))
    p = pairs[0]
    assert [s["lap_dist_m"] for s in p.samples_a] == [200.0]
    assert [s["lap_dist_m"] for s in p.samples_b] == [250.0]


def test_excludes_samples_missing_lap_dist() -> None:
    """Samples without lap_dist_m can't be placed on the track —
    skip them silently (matching the sector_splits pattern)."""
    a = [_s(1, 0, 200.0), _s(1, 100, None), _s(1, 200, 350.0)]
    b = [_s(1, 0, 250.0), _s(1, 100, 380.0)]
    pairs = segment_paired_laps(a, b, corners=(T1,))
    p = pairs[0]
    assert [s["lap_dist_m"] for s in p.samples_a] == [200.0, 350.0]


def test_corner_unreached_yields_empty_samples_on_that_side() -> None:
    """A's lap covers T1 but B's lap stops before T1's start. The
    pair is still emitted (so consumers can detect 'no data on B
    side for this corner'), but B's segment is empty."""
    a = [_s(1, 0, 200.0)]  # in T1
    b = [_s(1, 0, 50.0)]  # before T1
    pairs = segment_paired_laps(a, b, corners=(T1,))
    assert len(pairs) == 1
    p = pairs[0]
    assert len(p.samples_a) == 1
    assert len(p.samples_b) == 0


# -- corner geometry ---------------------------------------------


def test_overlapping_corners_share_samples() -> None:
    """Corner windows are independent — the same sample appearing
    in two overlapping corners shows up in both segments. This is
    intentional; the segmenter doesn't validate corner geometry."""
    overlap_a = Corner(name="T1a", start_m=100.0, end_m=400.0)
    overlap_b = Corner(name="T1b", start_m=200.0, end_m=500.0)
    a = [_s(1, 0, 250.0)]  # in both
    b = [_s(1, 0, 300.0)]  # in both
    pairs = segment_paired_laps(a, b, corners=(overlap_a, overlap_b))
    assert all(len(p.samples_a) == 1 for p in pairs)
    assert all(len(p.samples_b) == 1 for p in pairs)


def test_segment_window_is_inclusive_at_both_ends() -> None:
    """A sample exactly at start_m or end_m is *in* the corner.
    Pinning this so a future change to half-open windows trips
    the test (and forces a re-think of the divergence-point
    track_distance_m semantics)."""
    a = [
        _s(1, 0, 100.0),  # exactly start
        _s(1, 100, 400.0),  # exactly end
        _s(1, 200, 401.0),  # just past
    ]
    b = [_s(1, 0, 200.0)]
    pairs = segment_paired_laps(a, b, corners=(T1,))
    p = pairs[0]
    assert [s["lap_dist_m"] for s in p.samples_a] == [100.0, 400.0]


# -- shape ---------------------------------------------------------


def test_returns_paired_lap_segment_dataclass() -> None:
    """The return type is the public dataclass — locked so the
    downstream T034 cross-correlation can rely on the field
    names without reaching into a dict."""
    a = [_s(1, 0, 200.0)]
    b = [_s(1, 0, 250.0)]
    pairs = segment_paired_laps(a, b, corners=(T1,))
    assert isinstance(pairs[0], PairedLapSegment)
