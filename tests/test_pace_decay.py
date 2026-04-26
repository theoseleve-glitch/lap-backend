"""T071 (sprint2-backend) — pace-decay score.

Pure-domain function. Returns the OLS linear-regression slope of
lap times against sequential lap index across a stint, in
seconds-per-lap. Positive slope = pace decaying (lap times
getting slower); negative = improving.

Why slope, not "delta last - first": a single noisy lap at
either end would dominate. OLS uses every point and is the
standard sim-engineering metric for tire/fuel wear.

Why seconds-per-lap (not s/s): the lap is the natural step
unit — engineers cite \"0.05 s/lap decay\" not \"0.0005 s/s\".
Per-second would force divide-by-laptime conversion on every
read.

Stored on T069's StintRecord.lap_time_decay_seconds. T072 reads
this for tire-preservation scoring.

Edge cases pinned:
  - len < 2 → None (regression undefined for one point)
  - perfectly flat → 0.0
  - perfect linear up → exact known slope
  - perfect linear down → exact negative slope
"""

from __future__ import annotations

import math

import pytest

from domain.pace_decay import compute_pace_decay_seconds_per_lap

# -- happy paths --------------------------------------------------


def test_flat_pace_yields_zero_slope() -> None:
    """Identical lap times across the stint → 0.0 slope.
    Pin the no-decay case — without it a downstream "if decay > 0"
    branch could miss perfectly preserved tires."""
    result = compute_pace_decay_seconds_per_lap(
        lap_times_seconds=[103.5, 103.5, 103.5, 103.5, 103.5],
    )
    assert result == 0.0


def test_perfect_linear_increase_returns_known_slope() -> None:
    """Lap times [100, 100.5, 101, 101.5, 102] — uniform 0.5 s/lap
    increase. OLS on a perfectly linear series returns exactly the
    underlying slope. Pin: 0.5, not 0.0 (no slope) and not 2.0 (run
    over rise inverted)."""
    result = compute_pace_decay_seconds_per_lap(
        lap_times_seconds=[100.0, 100.5, 101.0, 101.5, 102.0],
    )
    assert result is not None
    assert math.isclose(result, 0.5, rel_tol=1e-9)


def test_perfect_linear_decrease_returns_negative_slope() -> None:
    """Improving stint (track rubbering in, fuel burning off):
    lap times [105, 104, 103, 102, 101] → slope -1.0 s/lap.
    Negative result must come back negative — pin the sign
    convention (positive = decay, negative = improvement)."""
    result = compute_pace_decay_seconds_per_lap(
        lap_times_seconds=[105.0, 104.0, 103.0, 102.0, 101.0],
    )
    assert result is not None
    assert math.isclose(result, -1.0, rel_tol=1e-9)


def test_two_lap_stint_returns_simple_difference() -> None:
    """Minimum N for OLS: 2 points define a line exactly. Lap
    times [100.0, 100.7] → slope 0.7 s/lap. Pin the lower bound
    so a 2-lap FCY-shortened stint still produces a number, not
    None."""
    result = compute_pace_decay_seconds_per_lap(
        lap_times_seconds=[100.0, 100.7],
    )
    assert result is not None
    assert math.isclose(result, 0.7, rel_tol=1e-9)


# -- degenerate inputs --------------------------------------------


def test_single_lap_returns_none() -> None:
    """One lap → slope undefined (need 2+ points). Return None
    so downstream callers branch on \"no decay yet\" rather than
    silently storing math.nan in the StintRecord float column."""
    assert (
        compute_pace_decay_seconds_per_lap(lap_times_seconds=[100.0])
        is None
    )


def test_empty_stint_returns_none() -> None:
    """No laps → None. Same rationale as single-lap."""
    assert (
        compute_pace_decay_seconds_per_lap(lap_times_seconds=[])
        is None
    )


def test_negative_lap_time_raises() -> None:
    """Lap times must be positive seconds. A negative value is a
    caller-side parse bug; raise to surface it instead of producing
    a meaningless slope."""
    with pytest.raises(ValueError, match="lap_times_seconds"):
        compute_pace_decay_seconds_per_lap(
            lap_times_seconds=[100.0, -1.0, 102.0],
        )
