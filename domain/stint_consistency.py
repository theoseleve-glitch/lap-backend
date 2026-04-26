"""T070 (sprint2-backend) — long-run consistency score.

Pure-domain function. Returns the sample standard deviation of
the LAST `window` lap times in a stint, or None if there isn't
enough data.

Why last-N (not full-stint): opening laps of a stint are skewed
by the out-lap (cold tires, traffic exit) — full-stint stddev
mistakes warm-up variance for inconsistency. The closing window
captures steady-state pace.

Why sample stddev (statistics.stdev, divisor N-1) and not
population: sim-racing convention. Single-stint comparisons
against cohort benchmarks should be unbiased estimators.
Switching to pstdev would shrink the reported variance and
bias percentiles upward relative to historical baselines.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence


def compute_stint_consistency_seconds(
    *,
    lap_times_seconds: Sequence[float],
    window: int,
) -> float | None:
    """Return sample stddev of the last `window` lap times, or None
    when there are fewer than 2 laps available (stddev undefined).

    If `window` exceeds the number of available laps, the full
    stint is used — short stints (FCY-shortened, end-of-race)
    must not raise; otherwise every caller would have to bound-
    check.
    """
    if window < 2:
        raise ValueError(
            f"window must be >= 2 for sample stddev; got {window}"
        )
    if len(lap_times_seconds) < 2:
        return None
    closing = list(lap_times_seconds[-window:])
    return statistics.stdev(closing)
