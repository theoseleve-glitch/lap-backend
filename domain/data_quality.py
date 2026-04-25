"""DataQuality scorer (T120).

Per ADR-006's `DataQuality` schema, this module produces the three
input metrics — channel completeness, gaps count, sample-rate
stability — and the composite 0–100 score derived from them.

Score formula (each penalty saturates):
    100
    - 50 * (1 - channel_completeness)         # up to 50 off
    - 5  * min(sample_gaps_count, 6)          # up to 30 off
    - 100 * min(sample_rate_stability, 0.2)   # up to 20 off
Result is rounded to int and clamped to [0, 100].
"""

import statistics
from collections.abc import Iterable
from dataclasses import dataclass

_DEFAULT_GAP_THRESHOLD_MS = 200
_GAP_SATURATE_AT = 6
_RATE_STABILITY_SATURATE_AT = 0.2


@dataclass
class DataQualityScore:
    score: int
    channel_completeness: float
    sample_gaps_count: int
    sample_rate_stability: float


def score_data_quality(
    samples: Iterable[dict[str, object]],
    expected_channels: set[str],
    gap_threshold_ms: int = _DEFAULT_GAP_THRESHOLD_MS,
) -> DataQualityScore:
    timestamps_ns: list[int] = []
    filled_cells = 0
    total_cells = 0

    for sample in samples:
        t_ns = sample.get("t_canonical_ns")
        if isinstance(t_ns, int):
            timestamps_ns.append(t_ns)
        for channel in expected_channels:
            total_cells += 1
            if channel in sample:
                filled_cells += 1

    if not timestamps_ns:
        return DataQualityScore(
            score=0,
            channel_completeness=0.0,
            sample_gaps_count=0,
            sample_rate_stability=0.0,
        )

    completeness = filled_cells / total_cells if total_cells > 0 else 1.0

    dts = [b - a for a, b in zip(timestamps_ns, timestamps_ns[1:], strict=False)]
    threshold_ns = gap_threshold_ms * 1_000_000
    gaps_count = sum(1 for dt in dts if dt > threshold_ns)

    if len(dts) >= 2:
        mean_dt = statistics.mean(dts)
        stability = (
            statistics.stdev(dts) / mean_dt if mean_dt > 0 else 0.0
        )
    else:
        stability = 0.0

    deductions = (
        50.0 * (1.0 - completeness)
        + 5.0 * min(gaps_count, _GAP_SATURATE_AT)
        + 100.0 * min(stability, _RATE_STABILITY_SATURATE_AT)
    )
    score = max(0, min(100, round(100.0 - deductions)))

    return DataQualityScore(
        score=score,
        channel_completeness=completeness,
        sample_gaps_count=gaps_count,
        sample_rate_stability=stability,
    )
