"""T072 (sprint2-backend) — tire preservation score.

Rule-based [0, 1] scalar combining two stint signals:
  - fuel_used_per_lap (liters/lap): heavy fuel use ≈ aggressive
    throttle, which thermal-loads tires.
  - pace_decay_seconds_per_lap (T071 OLS slope): positive value =
    pace degrading (thermal/wear), negative = improving.

Composition is MIN of the two sub-scores. A driver with great
pace consistency but terrible fuel burn isn't preserving tires —
they are just consistent at being aggressive. Average would hide
the imbalance; min punishes the weaker dimension.

Constants live at module scope (not function args) because T077
endurance insights reference these thresholds — keeping them in
one place keeps the rule explainable. Values come from a survey
of GT3-style endurance practice telemetry; they will be tuned
once the cohort percentile data lands.
"""

from __future__ import annotations

# Ceiling for fuel burn (liters/lap) — at or above this counts as
# 0.0 on the fuel sub-score. Survey baseline for GT3 endurance
# practice; refined by cohort once T078 lands.
MAX_FUEL_PER_LAP = 4.5

# Ceiling for pace decay (seconds/lap slope) — at or above this
# counts as 0.0 on the decay sub-score. Half a second per lap is
# the threshold where engineers tag a stint as "tire-limited."
MAX_DECAY_PER_LAP = 0.5


def compute_tire_preservation_score(
    *,
    fuel_used_per_lap: float,
    pace_decay_seconds_per_lap: float,
) -> float:
    """Return a [0, 1] tire-preservation score.

    1.0 = perfect preservation (zero fuel + zero decay).
    0.0 = either signal at or beyond its configured ceiling.
    Linear interpolation between, then MIN of the two sub-scores.
    """
    if fuel_used_per_lap < 0:
        raise ValueError(
            f"fuel_used_per_lap must be non-negative; got {fuel_used_per_lap}"
        )
    fuel_sub = max(0.0, 1.0 - fuel_used_per_lap / MAX_FUEL_PER_LAP)
    # Negative decay (improving) clamps at the perfect 1.0 sub-score
    # so impossibly-good pace can't mask fuel abuse.
    decay_normalized = max(0.0, pace_decay_seconds_per_lap)
    decay_sub = max(0.0, 1.0 - decay_normalized / MAX_DECAY_PER_LAP)
    return min(fuel_sub, decay_sub)
