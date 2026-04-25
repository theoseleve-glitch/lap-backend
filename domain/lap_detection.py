"""Lap detection from a sample stream (T117).

Walks samples in chronological order, identifies lap_number
transitions, and yields a `LapRecord` for each *completed* lap (a
lap is complete iff the stream contains both its first sample and
the first sample of the next lap — the in-progress final lap is
not emitted because we don't know its duration yet).

Track length is currently advisory: implausibly short or long laps
are flagged `valid=False` rather than discarded so downstream
analytics can see and reason about them.
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from domain.track_layout import TrackLayout

_MIN_PLAUSIBLE_LAP_MS = 60_000  # 1-minute floor — covers every real circuit
_MAX_PLAUSIBLE_LAP_MS = 24 * 60 * 60 * 1000  # >1 day = clock skew, not a lap


@dataclass
class LapRecord:
    lap_number: int
    time_ms: int
    valid: bool


def detect_laps(
    samples: Iterable[dict[str, object]],
    track_layout: TrackLayout,
) -> Iterator[LapRecord]:
    _ = track_layout  # length is advisory; reserved for T118 sectorization
    transitions: list[tuple[int, int]] = []  # (lap_number, t_canonical_ns)
    for sample in samples:
        lap_number = sample.get("lap_number")
        if not isinstance(lap_number, int):
            continue
        t_canonical_ns = sample.get("t_canonical_ns")
        if not isinstance(t_canonical_ns, int):
            continue
        if transitions and transitions[-1][0] == lap_number:
            continue  # same lap continuing — not a transition
        transitions.append((lap_number, t_canonical_ns))

    for (lap_number, t_start), (_, t_end) in zip(
        transitions, transitions[1:], strict=False
    ):
        time_ms = (t_end - t_start) // 1_000_000
        valid = _MIN_PLAUSIBLE_LAP_MS <= time_ms <= _MAX_PLAUSIBLE_LAP_MS
        yield LapRecord(lap_number=lap_number, time_ms=time_ms, valid=valid)
