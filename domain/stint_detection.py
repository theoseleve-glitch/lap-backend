"""T068 (sprint2-backend) — stint detection from pit-event markers.

Pure-domain function. Splits a long session's lap range into stints
given the laps where the driver pitted in. The caller decides what
counts as a pit (sim event, telemetry channel, manual log) and just
hands us the resulting lap numbers — keeps the detector forward-
compatible with whatever pit-source lands in T097's StrategyEvent.

Boundary convention: a pit at lap N closes the current stint at N
(the in-lap is the last lap of that stint). The next stint begins
at N+1 unless N == total_laps (final-lap pit just ends the session
without an empty trailing stint).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Stint:
    """A contiguous run of laps between two pit events (or session
    ends). All bounds inclusive; lap numbers are 1-indexed to match
    the Lap.lap_number convention.
    """

    start_lap: int
    end_lap: int
    lap_count: int


def detect_stints(
    *, total_laps: int, pit_in_laps: Sequence[int]
) -> list[Stint]:
    """Return the stints implied by `pit_in_laps` over `total_laps`.

    Empty session (`total_laps == 0`) → empty list.
    No pits → one stint covering [1, total_laps].
    Otherwise → one stint per pit, plus one trailing stint if the
    last pit isn't on the final lap.
    """
    if total_laps < 0:
        raise ValueError(f"total_laps must be >= 0, got {total_laps}")
    if total_laps == 0:
        return []

    # Validate pit_in_laps up-front; refuse rather than producing
    # malformed stints that would corrupt downstream long-run metrics.
    prev = 0
    for pit_lap in pit_in_laps:
        if pit_lap < 1 or pit_lap > total_laps:
            raise ValueError(
                f"pit_in_laps entry {pit_lap} out of range "
                f"[1, {total_laps}]"
            )
        if pit_lap <= prev:
            raise ValueError(
                f"pit_in_laps must be strictly ascending; "
                f"saw {pit_lap} after {prev}"
            )
        prev = pit_lap

    stints: list[Stint] = []
    start = 1
    for pit_lap in pit_in_laps:
        stints.append(
            Stint(
                start_lap=start,
                end_lap=pit_lap,
                lap_count=pit_lap - start + 1,
            )
        )
        start = pit_lap + 1
    if start <= total_laps:
        stints.append(
            Stint(
                start_lap=start,
                end_lap=total_laps,
                lap_count=total_laps - start + 1,
            )
        )
    return stints
