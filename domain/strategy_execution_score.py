"""T102 (sprint2-backend) — strategy execution score (0-100).

Pure-domain function. Reduces T101's StrategyDivergence into a
single 0-100 integer score for one session. Higher = better
execution (closer to plan).

Formula:
  pit_norm   = min(1, pit_lap_delta / total_race_laps) if total else 0
  fuel_norm  = min(1, fuel_delta / planned_fuel)       if planned else 0
  stint_norm = min(1, stint_delta / total_race_laps)   if total else 0

  weighted = 0.50 * pit_norm + 0.25 * fuel_norm + 0.25 * stint_norm
  score    = round(100 * max(0, 1 - weighted))

Weighting bias (0.50 / 0.25 / 0.25) reflects visibility:
pit-lap mismatches are the most directly actionable strategic
miss, fuel and stint duration are slower-burning consequences
of upstream choices.

Score is clamped [0, 100] and returned as int — the persistence
column is INTEGER; an accidental float would force SQLAlchemy
type coercion that silently rounds at the storage layer rather
than the explicit rounding done here.
"""

from __future__ import annotations

from domain.strategy_divergence import StrategyDivergence

_W_PIT = 0.50
_W_FUEL = 0.25
_W_STINT = 0.25


def compute_strategy_execution_score(
    *,
    divergence: StrategyDivergence,
    planned_fuel_liters: float,
    total_race_laps: int,
) -> int:
    """Compute the 0-100 execution score for a session.

    Empty plan (planned_fuel_liters=0 AND total_race_laps=0)
    returns 100 — nothing to diverge from.

    Caps each delta at 1.0 against its theoretical worst case
    so pathological inputs don't underflow past 0.
    """
    pit_norm = (
        min(1.0, divergence.pit_lap_delta_total / total_race_laps)
        if total_race_laps > 0
        else 0.0
    )
    fuel_norm = (
        min(1.0, divergence.fuel_delta_liters / planned_fuel_liters)
        if planned_fuel_liters > 0
        else 0.0
    )
    stint_norm = (
        min(1.0, divergence.stint_duration_delta_laps / total_race_laps)
        if total_race_laps > 0
        else 0.0
    )
    weighted = _W_PIT * pit_norm + _W_FUEL * fuel_norm + _W_STINT * stint_norm
    score = max(0.0, 1.0 - weighted)
    return int(round(100 * score))
