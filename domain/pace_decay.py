"""T071 (sprint2-backend) — pace-decay score.

Pure-domain function. Returns OLS linear-regression slope of lap
times against sequential lap index across a stint, in
seconds-per-lap. Positive = pace decaying (slower with time),
negative = improving.

Why slope vs naive (last - first): a single noisy lap at either
end would dominate. OLS uses every point — standard sim-engineering
metric for tire/fuel wear.

Closed-form OLS slope: slope = sum((x-mean_x)*(y-mean_y))
                              / sum((x-mean_x)**2)

x is the sequential lap index 0..N-1, evenly spaced — var(x) is
fully determined by N. We compute the standard form (not the
shortcut) for clarity; perf is irrelevant at stint sizes (≤120).

Stored on T069's StintRecord.lap_time_decay_seconds; T072 reads
this for tire-preservation scoring.
"""

from __future__ import annotations

from collections.abc import Sequence


def compute_pace_decay_seconds_per_lap(
    *,
    lap_times_seconds: Sequence[float],
) -> float | None:
    """Return OLS slope of lap_times vs lap index, or None when
    fewer than 2 laps are available (slope undefined for one point).

    Positive = decay (slowdown), negative = improvement, 0.0 = flat.
    """
    if any(t < 0 for t in lap_times_seconds):
        raise ValueError(
            "lap_times_seconds must be non-negative; "
            "negative values usually indicate a parse bug"
        )
    n = len(lap_times_seconds)
    if n < 2:
        return None

    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(lap_times_seconds) / n

    numerator = sum(
        (x - mean_x) * (y - mean_y)
        for x, y in zip(xs, lap_times_seconds, strict=True)
    )
    denominator = sum((x - mean_x) ** 2 for x in xs)
    return numerator / denominator
