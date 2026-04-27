"""T102 (sprint2-backend) — strategy execution score (0-100).

Pure-domain function. Reduces T101's StrategyDivergence into a
single 0-100 integer score. Higher = better execution; 100 =
perfect (deltas all zero), 0 = catastrophic divergence.

Weighting (sum to 1.0):
  - 0.50 pit-lap (most visible execution miss; dominant signal)
  - 0.25 fuel (over/under fueling; partially recoverable)
  - 0.25 stint duration (slowest signal to manifest)

Per-axis normalization caps each delta against its theoretical
worst case before weighting:
  - pit_lap_delta / total_race_laps (cap 1.0)
  - fuel_delta / planned_fuel (cap 1.0; planned=0 contributes 0)
  - stint_duration_delta / total_race_laps (cap 1.0)

Score = round(100 * max(0, 1 - sum(normalized_delta * weight))).

Pinned behaviors:
  - zero deltas → 100 (perfect execution)
  - all deltas equal max → 0 (worst-case)
  - partial divergence → 1..99 (monotonic in deltas)
  - empty plan (planned_fuel=0, total_race_laps=0) → 100
    (nothing to diverge from is not a failure)
  - score is bounded [0, 100] (no overflow on weird inputs)
  - the function returns an int (not float); the storage column
    is INTEGER and float-rounding behavior must be deterministic
"""

from __future__ import annotations

from domain.strategy_divergence import StrategyDivergence
from domain.strategy_execution_score import (
    compute_strategy_execution_score,
)


def _div(
    *,
    pit: int = 0, fuel: float = 0.0, stint: int = 0,
) -> StrategyDivergence:
    return StrategyDivergence(
        pit_lap_delta_total=pit,
        fuel_delta_liters=fuel,
        stint_duration_delta_laps=stint,
    )


# -- perfect / empty -------------------------------------------


def test_zero_deltas_yield_perfect_score() -> None:
    """Plan executed perfectly — every delta is 0 → 100. Pin:
    the load-bearing happy path. Without this the score is
    meaningless as a quality metric."""
    score = compute_strategy_execution_score(
        divergence=_div(pit=0, fuel=0.0, stint=0),
        planned_fuel_liters=80.0,
        total_race_laps=60,
    )
    assert score == 100


def test_empty_plan_yields_perfect_score() -> None:
    """No plan to compare against (planned_fuel=0,
    total_race_laps=0). The score is 100, not a divide-by-zero
    crash. Pin: a regression that crashed on missing plan would
    silence the metric for every sprint-only session."""
    score = compute_strategy_execution_score(
        divergence=_div(pit=0, fuel=0.0, stint=0),
        planned_fuel_liters=0.0,
        total_race_laps=0,
    )
    assert score == 100


# -- catastrophic ---------------------------------------------


def test_max_deltas_yield_zero_score() -> None:
    """Pit delta == total_race_laps, fuel delta == planned_fuel,
    stint delta == total_race_laps → every normalized component
    saturates at 1 → weighted sum = 1 → score = 0. Pin: the
    bottom of the scale; the score must clamp at 0 even if
    the math would underflow further."""
    score = compute_strategy_execution_score(
        divergence=_div(pit=60, fuel=80.0, stint=60),
        planned_fuel_liters=80.0,
        total_race_laps=60,
    )
    assert score == 0


def test_score_clamps_at_zero_for_overflow_deltas() -> None:
    """Pathological case: deltas exceed their caps (e.g., pit
    delta > total_race_laps from greedy nearest-match against
    a long plan). Score must remain >= 0. Pin: a regression
    that returned negative would break the INT-NULL storage
    contract and confuse the UI."""
    score = compute_strategy_execution_score(
        divergence=_div(pit=999, fuel=999.0, stint=999),
        planned_fuel_liters=10.0,
        total_race_laps=10,
    )
    assert score == 0


# -- partial / monotonic --------------------------------------


def test_better_execution_scores_strictly_higher_than_worse() -> None:
    """Two divergence values, one strictly larger than the
    other on every axis. The worse divergence must score
    strictly lower. Pin: monotonicity is the contract a UI
    can rely on — "improving any axis improves your score"."""
    better = compute_strategy_execution_score(
        divergence=_div(pit=2, fuel=5.0, stint=3),
        planned_fuel_liters=80.0,
        total_race_laps=60,
    )
    worse = compute_strategy_execution_score(
        divergence=_div(pit=20, fuel=40.0, stint=30),
        planned_fuel_liters=80.0,
        total_race_laps=60,
    )
    assert better > worse


def test_pit_lap_delta_dominates_score_more_than_fuel() -> None:
    """Same magnitude divergence on pit-lap vs fuel — the
    pit-lap weight is 0.50 and fuel is 0.25, so pit-lap
    deviation costs roughly twice as many score points. Pin:
    a regression to equal weights would conflate the
    "execution miss" (pit) and "supporting choice" (fuel)
    signals."""
    pit_only = compute_strategy_execution_score(
        divergence=_div(pit=10, fuel=0.0, stint=0),
        planned_fuel_liters=80.0,
        total_race_laps=60,
    )
    fuel_only = compute_strategy_execution_score(
        divergence=_div(pit=0, fuel=10.0, stint=0),
        planned_fuel_liters=80.0,
        total_race_laps=60,
    )
    # Pit/total = 10/60; weighted = 0.083.  Fuel/planned = 10/80; weighted = 0.031.
    # → Pit-only is the bigger hit, so its score is LOWER.
    assert pit_only < fuel_only


# -- type ----------------------------------------------------


def test_return_type_is_int() -> None:
    """Storage column is INTEGER; an accidental float return
    would silently truncate or fail SQLAlchemy's coercion in
    edge cases. Pin: explicit rounding to int."""
    score = compute_strategy_execution_score(
        divergence=_div(pit=3, fuel=7.0, stint=4),
        planned_fuel_liters=80.0,
        total_race_laps=60,
    )
    assert isinstance(score, int)
