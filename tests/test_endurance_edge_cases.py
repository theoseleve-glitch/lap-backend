"""T080 (sprint2-backend) — endurance pipeline edge cases.

T079 pinned the happy path (49-lap, 2-stint, ~90-min session). This
file pins the boundary contracts the pipeline quietly relies on:

  - single-stint sessions (no pits at all): T068 returns one stint
    covering [1, total_laps] and the downstream scorers all return
    real numbers — no special-case the caller has to write.
  - variable stint length in one session: short + medium + long
    stints in the same session are scored independently. A future
    bug that pools stints together (e.g., reusing the last stint's
    closing window across the whole session) gets caught here.
  - very short stints (FCY-shortened, 2-3 laps): consistency must
    not raise when `window > stint_length` — T070 documents this
    "callers must not bound-check" contract; a regression that
    hardens the bounds would break short-stint scoring silently.
  - single-lap session: per-stint decay/consistency return None
    (T070 stddev needs ≥2 points, T071 OLS slope needs ≥2 points).
    Pin so a regression that returns 0.0 for "no data" doesn't
    silently flatten the metric.
  - last-lap pit: T068's "no empty trailing stint" contract. A
    pit on the final lap closes the session cleanly; the next
    stint would be empty so we omit it. Without this pin, a future
    refactor that always appends a trailing stint would yield a
    zero-lap stint that crashes downstream scorers.

These cases are integration-flavored (they compose T068 + T070 +
T071 + T072) but use deterministic synthetic lap series — exact
expected values are hand-checkable, no fixtures required.
"""

from __future__ import annotations

import pytest

from domain.pace_decay import compute_pace_decay_seconds_per_lap
from domain.stint_consistency import compute_stint_consistency_seconds
from domain.stint_detection import Stint, detect_stints
from domain.tire_preservation import compute_tire_preservation_score

# -- single-stint session -----------------------------------------


def test_single_stint_session_with_no_pits_returns_one_stint() -> None:
    """A session that never pits (sprint, short qualifier) gets one
    stint covering every lap. Pin: a regression that requires at
    least one pit to emit a stint would break sprint scoring."""
    stints = detect_stints(total_laps=15, pit_in_laps=[])
    assert stints == [Stint(start_lap=1, end_lap=15, lap_count=15)]


def test_single_stint_pipeline_returns_real_numbers() -> None:
    """No-pit, ≥2-lap session must produce real (non-None) decay
    and consistency, and a tire_preservation in [0, 1]. Pin: every
    scorer must compose without the caller branching on stint count."""
    lap_times = [110.0 + 0.1 * i for i in range(12)]
    stints = detect_stints(total_laps=12, pit_in_laps=[])
    assert len(stints) == 1
    decay = compute_pace_decay_seconds_per_lap(lap_times_seconds=lap_times)
    consistency = compute_stint_consistency_seconds(
        lap_times_seconds=lap_times, window=10,
    )
    assert decay is not None
    assert consistency is not None
    score = compute_tire_preservation_score(
        fuel_used_per_lap=3.5,
        pace_decay_seconds_per_lap=decay,
    )
    assert 0.0 <= score <= 1.0


# -- variable stint length ----------------------------------------


def test_variable_stint_lengths_are_scored_independently() -> None:
    """A 36-lap session with stints of 3, 8, and 25 laps gets
    three independently-scored stints — the long stint's decay
    must not influence the short stint's score (and vice-versa).

    Pin via inequality: stint A is a clean short run (essentially
    flat), stint C is a long aggressive run with strong positive
    decay. If the pipeline pooled stints, A's decay would inherit
    C's heavy slope.
    """
    stint_a = [110.0, 110.05, 110.1]
    stint_b = [110.0 + 0.02 * i for i in range(8)]
    stint_c = [110.0 + 0.30 * i for i in range(25)]
    full = stint_a + stint_b + stint_c
    stints = detect_stints(total_laps=36, pit_in_laps=[3, 11])
    assert [s.lap_count for s in stints] == [3, 8, 25]

    decays = [
        compute_pace_decay_seconds_per_lap(
            lap_times_seconds=full[s.start_lap - 1 : s.end_lap]
        )
        for s in stints
    ]
    assert all(d is not None for d in decays)
    decay_a, decay_b, decay_c = decays
    assert decay_a is not None and decay_c is not None
    assert decay_c > decay_a + 0.20, (
        f"long aggressive stint must be markedly steeper than the "
        f"short clean stint; got short={decay_a:.3f}, long={decay_c:.3f}"
    )


# -- short stint, window larger than stint length -----------------


def test_short_stint_consistency_uses_full_stint_when_window_oversized() -> None:
    """T070 contract: if `window` exceeds the stint's lap count,
    the function uses the full stint instead of raising. Endurance
    sessions routinely produce 2-3 lap FCY-shortened stints; the
    scorer must absorb that without every caller bound-checking.

    Equality with the full-stint stddev pins the contract: the
    function must compute over min(window, len(laps)).
    """
    short_stint = [110.0, 111.0, 109.5]
    full_stddev = compute_stint_consistency_seconds(
        lap_times_seconds=short_stint, window=3,
    )
    oversized_stddev = compute_stint_consistency_seconds(
        lap_times_seconds=short_stint, window=50,
    )
    assert full_stddev is not None
    assert oversized_stddev == full_stddev


# -- single-lap session -------------------------------------------


def test_single_lap_session_per_stint_metrics_return_none() -> None:
    """A single-lap session (race red-flagged on the formation lap,
    or a one-lap qualifier) yields one stint of length 1. Per-stint
    decay needs ≥2 points (slope undefined), per-stint consistency
    needs ≥2 points (stddev undefined). Both must return None, not
    0.0 — a downstream "is this stint scored?" check relies on it.
    """
    stints = detect_stints(total_laps=1, pit_in_laps=[])
    assert stints == [Stint(start_lap=1, end_lap=1, lap_count=1)]
    one_lap = [108.5]
    assert (
        compute_pace_decay_seconds_per_lap(lap_times_seconds=one_lap)
        is None
    )
    assert (
        compute_stint_consistency_seconds(
            lap_times_seconds=one_lap, window=10,
        )
        is None
    )


# -- last-lap pit -------------------------------------------------


def test_pit_on_final_lap_closes_session_without_empty_trailing_stint() -> None:
    """T068 contract: a pit on lap == total_laps closes the session
    cleanly (the in-lap is the closing lap of the final stint). No
    empty trailing stint. Pin: a regression that always appends a
    trailing stint would emit a zero-lap stint here, which would
    crash every downstream scorer with insufficient-data Nones at
    best, integer-division-by-zero at worst.
    """
    stints = detect_stints(total_laps=20, pit_in_laps=[10, 20])
    assert stints == [
        Stint(start_lap=1, end_lap=10, lap_count=10),
        Stint(start_lap=11, end_lap=20, lap_count=10),
    ]
    assert all(s.lap_count > 0 for s in stints)


# -- malformed input rejection ------------------------------------


def test_pit_after_total_laps_raises() -> None:
    """Pin T068's input validation: pit numbers beyond total_laps
    are a parse bug upstream, not a scenario to silently absorb.
    Refusing here prevents corrupted stints from polluting the
    long-run cohort metrics."""
    with pytest.raises(ValueError):
        detect_stints(total_laps=10, pit_in_laps=[12])


def test_unsorted_pit_in_laps_raises() -> None:
    """Pin: pit_in_laps must be strictly ascending. Out-of-order
    pits would produce overlapping stints — better to fail loud
    than to publish a percentile against junk."""
    with pytest.raises(ValueError):
        detect_stints(total_laps=20, pit_in_laps=[10, 5])
