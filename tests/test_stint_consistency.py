"""T070 (sprint2-backend) — long-run consistency score.

Pure-domain function. Given a stint's lap times (chronological,
seconds), compute the standard deviation of the LAST N laps. The
last-N window matters because the opening laps of a stint are
often skewed by the out-lap (cold tires, traffic exit) — a
stddev across the full stint would mistake warm-up variance for
inconsistency.

Why sample stddev (divisor N-1) and not population: sim-racing
convention. Single-stint comparisons against a cohort benchmark
should be unbiased estimators; that's what statistics.stdev does
out of the box. Switching to population stddev would slightly
shrink reported variance and bias drivers' percentiles upward
relative to historical benchmarks.

Edge cases pinned by the tests:
  - len(lap_times) < 2 → None (stddev undefined for one sample)
  - window larger than the stint → use the full stint
  - identical laps → 0.0 (perfectly consistent)
"""

from __future__ import annotations

import math

import pytest

from domain.stint_consistency import compute_stint_consistency_seconds

# -- happy paths --------------------------------------------------


def test_uniform_pace_yields_zero_stddev() -> None:
    """A perfectly consistent stint (every lap identical) returns
    exactly 0.0, not a small floating-point near-zero. Pin the
    base case — without it a downstream percentile-rank query
    might filter ">0" and miss the perfect-consistency drivers."""
    result = compute_stint_consistency_seconds(
        lap_times_seconds=[105.2] * 12,
        window=10,
    )
    assert result == 0.0


def test_known_lap_times_reproduce_sample_stddev() -> None:
    """Sample stddev of [100, 101, 102, 103, 104] is sqrt(2.5)
    ≈ 1.5811. Pin the formula choice (sample, not population);
    if someone switches to statistics.pstdev the value becomes
    sqrt(2.0) ≈ 1.4142."""
    result = compute_stint_consistency_seconds(
        lap_times_seconds=[100.0, 101.0, 102.0, 103.0, 104.0],
        window=5,
    )
    assert result is not None
    assert math.isclose(result, math.sqrt(2.5), rel_tol=1e-9)


def test_window_truncates_to_last_n_laps() -> None:
    """First three laps are warm-up garbage (110, 109, 108);
    closing five are the steady pace (100, 101, 100, 102, 100).
    With window=5 the result is the stddev of just the closing
    five — the warm-up laps must NOT bleed in. Pin the slicing
    or the metric becomes meaningless on long stints."""
    closing = [100.0, 101.0, 100.0, 102.0, 100.0]
    expected = math.sqrt(
        sum((x - sum(closing) / 5) ** 2 for x in closing) / (5 - 1)
    )
    result = compute_stint_consistency_seconds(
        lap_times_seconds=[110.0, 109.0, 108.0, *closing],
        window=5,
    )
    assert result is not None
    assert math.isclose(result, expected, rel_tol=1e-9)


def test_window_larger_than_stint_uses_full_stint() -> None:
    """Stint of 4 laps, window=10 → use all 4. Pin the
    no-pad-no-error fallback — stints can be shorter than the
    standard window (e.g., FCY-shortened or end-of-race), and
    raising would force every caller to bound-check."""
    laps = [100.0, 101.0, 102.0, 103.0]
    expected = math.sqrt(
        sum((x - sum(laps) / 4) ** 2 for x in laps) / (4 - 1)
    )
    result = compute_stint_consistency_seconds(
        lap_times_seconds=laps,
        window=10,
    )
    assert result is not None
    assert math.isclose(result, expected, rel_tol=1e-9)


# -- degenerate inputs --------------------------------------------


def test_single_lap_returns_none() -> None:
    """One lap → stddev is undefined (sample stddev needs N>=2).
    Return None so downstream code can branch on "no consistency
    score yet" rather than crashing on a StatisticsError or
    silently storing math.nan in the StintRecord float column."""
    assert (
        compute_stint_consistency_seconds(
            lap_times_seconds=[100.0],
            window=10,
        )
        is None
    )


def test_empty_stint_returns_none() -> None:
    """No laps → None. Same rationale as single-lap; downstream
    callers must distinguish 'no data' from 'data shows 0 spread'."""
    assert (
        compute_stint_consistency_seconds(
            lap_times_seconds=[],
            window=10,
        )
        is None
    )


def test_window_below_two_raises() -> None:
    """window < 2 makes the metric mathematically meaningless
    (sample stddev needs >=2 samples). Refuse — silently
    floor-clamping would hide a caller bug at scale."""
    with pytest.raises(ValueError, match="window"):
        compute_stint_consistency_seconds(
            lap_times_seconds=[100.0, 101.0, 102.0],
            window=1,
        )
