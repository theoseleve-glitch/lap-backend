"""Session-level metric extraction (T119).

Pure function over T117's `LapRecord`s + a sample stream. Produces
the four scalars the sprint backlog calls out:
    best_lap_ms, avg_lap_ms, consistency_ms, peak_speed_kph

Lap-derived metrics use only `valid=True` laps so a glitchy
sub-minute "lap" doesn't show up as the session's fastest. Peak
speed reads the canonical `speed_mps` channel and converts to kph.
T122 will pick this up and persist it onto SessionRecord.
"""

import statistics
from collections.abc import Iterable
from dataclasses import dataclass

from domain.lap_detection import LapRecord

_MPS_TO_KPH = 3.6


@dataclass
class SessionMetrics:
    best_lap_ms: int | None = None
    avg_lap_ms: int | None = None
    consistency_ms: int | None = None
    peak_speed_kph: float | None = None


def extract_session_metrics(
    laps: Iterable[LapRecord],
    samples: Iterable[dict[str, object]],
) -> SessionMetrics:
    valid_times = [lap.time_ms for lap in laps if lap.valid]

    best_lap_ms = min(valid_times) if valid_times else None
    avg_lap_ms = (
        round(sum(valid_times) / len(valid_times)) if valid_times else None
    )
    consistency_ms = (
        round(statistics.stdev(valid_times)) if len(valid_times) >= 2 else None
    )

    peak_mps: float | None = None
    for sample in samples:
        speed = sample.get("speed_mps")
        if not isinstance(speed, int | float):
            continue
        if peak_mps is None or speed > peak_mps:
            peak_mps = float(speed)
    peak_speed_kph = (
        round(peak_mps * _MPS_TO_KPH, 2) if peak_mps is not None else None
    )

    return SessionMetrics(
        best_lap_ms=best_lap_ms,
        avg_lap_ms=avg_lap_ms,
        consistency_ms=consistency_ms,
        peak_speed_kph=peak_speed_kph,
    )
