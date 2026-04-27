"""T101 (sprint2-backend) — plan-vs-actual divergence scoring.

Pure-domain function. Reads a StrategyPlan-shaped intent and
the StrategyEvent-shaped runtime log; emits three deltas as
inputs to T102's 0-100 execution score:

  - pit_lap_delta_total: |actual_pit_lap - planned_pit_lap|
    summed across pairs. Each planned pit gets matched to
    the nearest actual pit (greedy left-to-right since the
    timeline is already lap-ordered). Planned pits with no
    actual counterpart contribute their full planned-lap
    number (the "didn't pit" sentinel is lap 0).
  - fuel_delta_liters: |sum(actual fuel_added) - planned fuel|.
    Missing `fuel_added_liters` in a pit event's details is
    treated as 0 (defensive — a tire-only stop may legitimately
    omit it).
  - stint_duration_delta_laps: sum of |actual_stint_laps -
    expected_stint_laps| where expected = total_race_laps /
    planned_stints. Each missing stint_end contributes the
    full expected length.

Out of scope: weighting / normalization / 0-100 score. Those
land in T102 so the weighting decisions don't poison the raw
deltas.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol


class _PlanLike(Protocol):
    planned_stints: int
    planned_fuel_liters: float
    planned_pit_laps: list[int]


class _EventLike(Protocol):
    kind: str
    at_lap: int
    details: dict[str, Any]


@dataclass(frozen=True)
class StrategyDivergence:
    """Three orthogonal divergence deltas. T102 weights and
    rolls them into a 0-100 execution score; the deltas
    themselves carry the raw signal."""

    pit_lap_delta_total: int
    fuel_delta_liters: float
    stint_duration_delta_laps: int


def compute_strategy_divergence(
    *,
    plan: _PlanLike,
    events: Iterable[_EventLike],
    total_race_laps: int,
) -> StrategyDivergence:
    """Compute the three plan-vs-actual deltas. Returns a
    StrategyDivergence record with absolute values (signed
    direction would lose information — late pits and early
    pits are both divergence)."""
    events_list = list(events)
    pit_events = [e for e in events_list if e.kind == "pit"]
    stint_end_events = [e for e in events_list if e.kind == "stint_end"]

    pit_delta = _pit_lap_delta(plan.planned_pit_laps, pit_events)
    fuel_delta = _fuel_delta(plan.planned_fuel_liters, pit_events)
    stint_delta = _stint_duration_delta(
        plan.planned_stints, stint_end_events, total_race_laps,
    )
    return StrategyDivergence(
        pit_lap_delta_total=pit_delta,
        fuel_delta_liters=fuel_delta,
        stint_duration_delta_laps=stint_delta,
    )


def _pit_lap_delta(
    planned: list[int], actual_pits: list[_EventLike],
) -> int:
    """Greedy pair-up: each planned pit consumes the nearest
    available actual pit. Unmatched planned pits contribute
    the full planned-lap value (vs sentinel 0)."""
    actual_laps = sorted(e.at_lap for e in actual_pits)
    total = 0
    for planned_lap in planned:
        if not actual_laps:
            total += planned_lap
            continue
        # Pick the actual pit with the smallest abs distance.
        best_idx = min(
            range(len(actual_laps)),
            key=lambda i: abs(actual_laps[i] - planned_lap),
        )
        total += abs(actual_laps[best_idx] - planned_lap)
        actual_laps.pop(best_idx)
    return total


def _fuel_delta(
    planned_fuel: float, pit_events: list[_EventLike],
) -> float:
    actual_fuel = sum(
        float(e.details.get("fuel_added_liters", 0) or 0)
        for e in pit_events
    )
    return abs(planned_fuel - actual_fuel)


def _stint_duration_delta(
    planned_stints: int,
    stint_end_events: list[_EventLike],
    total_race_laps: int,
) -> int:
    """Each missing stint_end event contributes the full
    expected stint length. Planned 0 stints + total 0 laps →
    no expectation, delta 0."""
    if planned_stints <= 0 or total_race_laps <= 0:
        return 0
    expected_per_stint = total_race_laps // planned_stints
    actual_count = len(stint_end_events)
    # Compare each expected stint to its actual length.
    actual_lengths = _stint_lengths(stint_end_events)
    total = 0
    for i in range(planned_stints):
        if i < actual_count:
            total += abs(actual_lengths[i] - expected_per_stint)
        else:
            # Missing stint_end → entire expected length missed.
            total += expected_per_stint
    return total


def _stint_lengths(stint_end_events: list[_EventLike]) -> list[int]:
    """Convert stint_end laps into per-stint lengths.
    Stint 1 = first end_lap; stint N = end_lap[N] - end_lap[N-1]."""
    laps = sorted(e.at_lap for e in stint_end_events)
    lengths: list[int] = []
    prev = 0
    for lap in laps:
        lengths.append(lap - prev)
        prev = lap
    return lengths
