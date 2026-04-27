"""T101 (sprint2-backend) — plan-vs-actual divergence scoring.

Pure-domain function. Given a StrategyPlan-shaped intent and a
StrategyEvent-shaped runtime log, computes three deltas:

  - pit_lap_delta_total: |actual_pit_lap - planned_pit_lap|
    summed across pit pairs (each planned pit is matched to its
    nearest actual pit)
  - fuel_delta_liters: |actual_fuel_used - planned_fuel|
    where actual_fuel_used = sum of pit-event details.fuel_added
  - stint_duration_delta_laps: sum of |actual_stint_laps -
    planned_stint_laps| where planned_stint_laps =
    total_race_laps / planned_stints (rough; spec is "delta",
    not a precise per-stint allocation)

T102 will roll these into a 0-100 execution score; T101 just
emits the raw deltas so T102's weighting decisions don't get
baked into the math.

Pinned behaviors:
  - empty plan + no events → all deltas 0
  - plan with no actual pits → pit_lap_delta_total = sum of
    planned pit laps (each planned pit incurs full delta vs
    "didn't pit" sentinel = 0)
  - actual pits exactly matching plan → pit_lap_delta_total = 0
  - actual pit two laps late → delta = 2
  - actual_fuel from pit details summed (kind=pit reads
    details.fuel_added_liters); plan minus actual gives the
    fuel delta
  - missing fuel_added_liters in details → treated as 0
    (defensive: caller may not always provide it)
  - actual stint count from stint_end events; if zero, the
    stint duration delta = sum of planned stint laps
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from domain.strategy_divergence import (
    StrategyDivergence,
    compute_strategy_divergence,
)


@dataclass(frozen=True)
class _Plan:
    planned_stints: int
    planned_fuel_liters: float
    planned_pit_laps: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class _Event:
    kind: str
    at_lap: int
    details: dict[str, Any] = field(default_factory=dict)


# -- empty / baseline -------------------------------------------


def test_no_plan_no_events_yields_zero_deltas() -> None:
    """Empty plan (0 stints, 0 fuel, no pits) and no events.
    Pin: a regression that defaulted to nonzero would surface
    spurious divergence on sessions with no strategy data."""
    result = compute_strategy_divergence(
        plan=_Plan(planned_stints=0, planned_fuel_liters=0.0),
        events=[],
        total_race_laps=0,
    )
    assert result.pit_lap_delta_total == 0
    assert result.fuel_delta_liters == 0.0
    assert result.stint_duration_delta_laps == 0


# -- pit-lap divergence -----------------------------------------


def test_actual_pits_matching_plan_yields_zero_pit_delta() -> None:
    """Plan: pit at lap 25. Actual: pit at lap 25. delta = 0.
    Pin: the load-bearing happy-path; perfect execution must
    score zero divergence."""
    result = compute_strategy_divergence(
        plan=_Plan(
            planned_stints=2, planned_fuel_liters=80.0,
            planned_pit_laps=[25],
        ),
        events=[
            _Event(kind="pit", at_lap=25, details={"fuel_added_liters": 40.0}),
        ],
        total_race_laps=50,
    )
    assert result.pit_lap_delta_total == 0


def test_actual_pit_two_laps_late_yields_pit_delta_of_two() -> None:
    """Plan: pit at 25. Actual: pit at 27. delta = 2. Pin:
    the absolute distance is the signal — pitting 2 laps late
    is the same magnitude error as pitting 2 laps early."""
    result = compute_strategy_divergence(
        plan=_Plan(
            planned_stints=2, planned_fuel_liters=80.0,
            planned_pit_laps=[25],
        ),
        events=[
            _Event(kind="pit", at_lap=27, details={"fuel_added_liters": 40.0}),
        ],
        total_race_laps=50,
    )
    assert result.pit_lap_delta_total == 2


def test_no_actual_pits_yields_pit_delta_equal_to_sum_of_planned(
) -> None:
    """Plan: pit at 25, 50. Actual: never pitted. delta =
    25 + 50 = 75 (each missed pit is the full distance from
    the planned lap to the "didn't happen" sentinel of lap 0).
    Pin: the function must not crash on missing actual pits;
    a plan that never executed is a meaningful score, not
    a divide-by-zero."""
    result = compute_strategy_divergence(
        plan=_Plan(
            planned_stints=3, planned_fuel_liters=120.0,
            planned_pit_laps=[25, 50],
        ),
        events=[],
        total_race_laps=80,
    )
    assert result.pit_lap_delta_total == 25 + 50


# -- fuel divergence --------------------------------------------


def test_fuel_added_summed_across_pits_for_actual_total(
) -> None:
    """Plan: 100L. Actual: two pits each add 50L → 100L total
    actual → fuel delta = 0. Pin: the function must sum
    fuel_added across pit events, not just read the first one."""
    result = compute_strategy_divergence(
        plan=_Plan(
            planned_stints=3, planned_fuel_liters=100.0,
            planned_pit_laps=[25, 50],
        ),
        events=[
            _Event(kind="pit", at_lap=25, details={"fuel_added_liters": 50.0}),
            _Event(kind="pit", at_lap=50, details={"fuel_added_liters": 50.0}),
        ],
        total_race_laps=75,
    )
    assert result.fuel_delta_liters == 0.0


def test_fuel_delta_when_actual_exceeds_plan(
) -> None:
    """Plan: 80L. Actual: 100L. delta = 20. Pin: signed
    direction would lose information here — both
    over-fueling (slow pit stops) and under-fueling
    (running dry) are divergence; we want the absolute
    magnitude."""
    result = compute_strategy_divergence(
        plan=_Plan(
            planned_stints=2, planned_fuel_liters=80.0,
            planned_pit_laps=[25],
        ),
        events=[
            _Event(kind="pit", at_lap=25, details={"fuel_added_liters": 100.0}),
        ],
        total_race_laps=50,
    )
    assert result.fuel_delta_liters == 20.0


def test_pit_event_without_fuel_added_treats_as_zero(
) -> None:
    """A pit event with no fuel_added_liters in details (e.g.,
    a tire-only stop logged minimally) → contributes 0 fuel.
    Pin: the function must not crash on incomplete details
    payloads; missing key is a domain reality."""
    result = compute_strategy_divergence(
        plan=_Plan(
            planned_stints=2, planned_fuel_liters=40.0,
            planned_pit_laps=[25],
        ),
        events=[
            _Event(kind="pit", at_lap=25, details={}),
        ],
        total_race_laps=50,
    )
    # Plan = 40, actual = 0 → delta = 40
    assert result.fuel_delta_liters == 40.0


# -- stint-duration divergence ----------------------------------


def test_stint_duration_delta_when_no_stint_ends(
) -> None:
    """Plan: 2 stints over 60 laps → expected 30 laps/stint
    each. Actual: zero stint_end events. delta = 30 + 30 = 60
    (each missing stint_end == full expected length missed).
    Pin: the absent-events case is the most common failure
    mode — a regression that crashed on empty would silence
    the metric for any non-completion."""
    result = compute_strategy_divergence(
        plan=_Plan(
            planned_stints=2, planned_fuel_liters=80.0,
            planned_pit_laps=[30],
        ),
        events=[],
        total_race_laps=60,
    )
    assert result.stint_duration_delta_laps == 60


def test_stint_duration_delta_zero_when_stints_match_plan(
) -> None:
    """Plan: 2 stints over 60 laps → 30/stint. Actual: stint_end
    events at lap 30 and 60 → 30-lap stints each. delta = 0."""
    result = compute_strategy_divergence(
        plan=_Plan(
            planned_stints=2, planned_fuel_liters=80.0,
            planned_pit_laps=[30],
        ),
        events=[
            _Event(kind="stint_end", at_lap=30),
            _Event(kind="stint_end", at_lap=60),
        ],
        total_race_laps=60,
    )
    assert result.stint_duration_delta_laps == 0


# -- structural ------------------------------------------------


def test_returns_strategy_divergence_dataclass(
) -> None:
    """Caller (T102 score, T100+ UI) needs the three fields
    by name. Pin: returning a tuple would force callers to
    track positional ordering."""
    result = compute_strategy_divergence(
        plan=_Plan(planned_stints=0, planned_fuel_liters=0.0),
        events=[],
        total_race_laps=0,
    )
    assert isinstance(result, StrategyDivergence)
    assert hasattr(result, "pit_lap_delta_total")
    assert hasattr(result, "fuel_delta_liters")
    assert hasattr(result, "stint_duration_delta_laps")
