"""T031 (sprint2-backend) — lap-aligned segmenter.

Bridge analysis needs comparable slices: for each (lap-pair, corner)
combination, the samples from session A and session B that fall
within the corner's track-distance window. This module does the
mechanical slicing; T034+ do the comparison.

Pairing policy is "by index": the 0th distinct lap of A is paired
with the 0th of B, the 1st with the 1st, etc. Truncate to the
shorter session — never pair an A-lap against a missing B-lap.
Selecting by lap time / "fastest lap" is a higher-layer policy
and not this module's concern.

Corner is a simple track-distance window `[start_m, end_m]`
(inclusive on both ends). Windows can overlap; the same sample
may appear in multiple corners. The segmenter doesn't validate
geometry.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Corner:
    name: str
    start_m: float
    end_m: float


@dataclass(frozen=True)
class PairedLapSegment:
    lap_pair_index: int
    corner_name: str
    samples_a: tuple[dict[str, object], ...]
    samples_b: tuple[dict[str, object], ...]


def _group_by_lap(
    samples: Iterable[dict[str, object]],
) -> list[list[dict[str, object]]]:
    """Walk the stream and partition into runs of contiguous-same
    lap_number, preserving order within each run. Samples missing
    a lap_number are skipped (matches the lap_detection convention)."""
    groups: list[list[dict[str, object]]] = []
    current_lap: object = None
    for sample in samples:
        lap = sample.get("lap_number")
        if not isinstance(lap, int):
            continue
        if lap != current_lap:
            groups.append([sample])
            current_lap = lap
        else:
            groups[-1].append(sample)
    return groups


def _slice_corner(
    lap_samples: list[dict[str, object]], corner: Corner
) -> tuple[dict[str, object], ...]:
    """Filter to samples in the corner's distance window. A sample
    without lap_dist_m can't be placed — skip it (consistent with
    sector_splits)."""
    out: list[dict[str, object]] = []
    for s in lap_samples:
        d = s.get("lap_dist_m")
        if not isinstance(d, int | float):
            continue
        if corner.start_m <= float(d) <= corner.end_m:
            out.append(s)
    return tuple(out)


def segment_paired_laps(
    samples_a: Iterable[dict[str, object]],
    samples_b: Iterable[dict[str, object]],
    corners: Sequence[Corner],
) -> list[PairedLapSegment]:
    laps_a = _group_by_lap(samples_a)
    laps_b = _group_by_lap(samples_b)
    n_pairs = min(len(laps_a), len(laps_b))
    out: list[PairedLapSegment] = []
    for i in range(n_pairs):
        for corner in corners:
            out.append(
                PairedLapSegment(
                    lap_pair_index=i,
                    corner_name=corner.name,
                    samples_a=_slice_corner(laps_a[i], corner),
                    samples_b=_slice_corner(laps_b[i], corner),
                )
            )
    return out
