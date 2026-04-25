"""Sector splits from a sample stream + TrackLayout (T118).

Sector boundaries on `TrackLayout` are a strictly-increasing tuple
of distances (metres from lap start). Two boundaries → three
sectors. The detector walks samples in chronological order and
yields one `SectorRecord` per completed sector.

A lap's sector records are buffered until the *next* lap's first
sample is observed: the in-progress final lap of the stream emits
nothing (no successor exists to close its final sector). Within a
completed lap, sectors that observed both their start and end
boundary are emitted; sectors broken by a data gap are dropped.
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from domain.track_layout import TrackLayout


@dataclass
class SectorRecord:
    lap_number: int
    sector_index: int
    time_ms: int


def compute_sector_times(
    samples: Iterable[dict[str, object]],
    track_layout: TrackLayout,
) -> Iterator[SectorRecord]:
    boundaries = track_layout.sector_boundaries_m
    if not boundaries:
        return

    current_lap: int | None = None
    sector_idx = 0
    sector_start_ns = 0
    buffered: list[SectorRecord] = []

    for sample in samples:
        lap_number = sample.get("lap_number")
        if not isinstance(lap_number, int):
            continue
        t_ns = sample.get("t_canonical_ns")
        if not isinstance(t_ns, int):
            continue

        if current_lap is None:
            current_lap = lap_number
            sector_idx = 0
            sector_start_ns = t_ns
        elif lap_number != current_lap:
            if sector_idx == len(boundaries):
                buffered.append(
                    SectorRecord(
                        lap_number=current_lap,
                        sector_index=sector_idx,
                        time_ms=(t_ns - sector_start_ns) // 1_000_000,
                    )
                )
            yield from buffered
            buffered = []
            current_lap = lap_number
            sector_idx = 0
            sector_start_ns = t_ns

        lap_dist = sample.get("lap_dist_m")
        if not isinstance(lap_dist, int | float):
            continue
        while (
            sector_idx < len(boundaries)
            and lap_dist >= boundaries[sector_idx]
        ):
            buffered.append(
                SectorRecord(
                    lap_number=current_lap,
                    sector_index=sector_idx,
                    time_ms=(t_ns - sector_start_ns) // 1_000_000,
                )
            )
            sector_start_ns = t_ns
            sector_idx += 1
