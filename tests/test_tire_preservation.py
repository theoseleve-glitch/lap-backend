"""T072 (sprint2-backend) — tire preservation score.

Rule-based [0, 1] scalar combining two stint signals:
  - fuel_used_per_lap (liters/lap): heavy fuel use ≈ aggressive
    throttle, which loads tires.
  - pace_decay_seconds_per_lap (T071's slope): positive = degrading.

Composition: MIN of the two sub-scores. A driver with great pace
consistency but terrible fuel burn isn't preserving tires; they
are just consistent at being aggressive. Average would hide that
imbalance — min punishes the weaker dimension.

Score interpretation:
  - 1.0 = both fuel + pace at floor (best preservation observed
    in normal racing)
  - 0.0 = either signal at or beyond the configured ceiling
  - linear interpolation between

Used by T077 endurance-specific insights ("tire abuse detected
in stint 2"). T069's StintRecord stores the underlying floats;
this function transforms them into the 0–1 dimension that lands
on T073's endurance-weighted DriverProgressSnapshot.
"""

from __future__ import annotations

import math

import pytest

from domain.tire_preservation import (
    MAX_DECAY_PER_LAP,
    MAX_FUEL_PER_LAP,
    compute_tire_preservation_score,
)

# -- happy paths --------------------------------------------------


def test_zero_fuel_and_zero_decay_score_is_one() -> None:
    """Floor on both inputs → perfect preservation (1.0). Pin
    the upper bound — without it, the scalar could quietly drift
    past 1.0 on optimistic inputs and break percentile mapping."""
    score = compute_tire_preservation_score(
        fuel_used_per_lap=0.0,
        pace_decay_seconds_per_lap=0.0,
    )
    assert score == 1.0


def test_at_or_above_max_fuel_score_is_zero() -> None:
    """Ceiling on fuel use → 0.0 regardless of how good decay is.
    Pin: high fuel burn alone (lots of throttle / aggressive style)
    is enough to flag the stint as abusive. Average-of-subscores
    would hide this."""
    score = compute_tire_preservation_score(
        fuel_used_per_lap=MAX_FUEL_PER_LAP,
        pace_decay_seconds_per_lap=0.0,  # pace is perfect
    )
    assert score == 0.0


def test_at_or_above_max_decay_score_is_zero() -> None:
    """Ceiling on pace decay → 0.0 regardless of fuel use. Pin
    the symmetric case: heavy decay (thermal degradation) alone
    flags the stint."""
    score = compute_tire_preservation_score(
        fuel_used_per_lap=0.0,  # fuel is perfect
        pace_decay_seconds_per_lap=MAX_DECAY_PER_LAP,
    )
    assert score == 0.0


def test_min_of_subscores_when_one_dimension_dominates() -> None:
    """fuel = 50% of ceiling (sub = 0.5), decay = 25% of ceiling
    (sub = 0.75). Final = MIN(0.5, 0.75) = 0.5. Pin the MIN
    composition — a switch to mean would yield 0.625, leaking
    fuel abuse behind decay performance."""
    score = compute_tire_preservation_score(
        fuel_used_per_lap=MAX_FUEL_PER_LAP * 0.5,
        pace_decay_seconds_per_lap=MAX_DECAY_PER_LAP * 0.25,
    )
    assert score is not None
    assert math.isclose(score, 0.5, rel_tol=1e-9)


def test_negative_decay_clamps_to_perfect_decay_subscore() -> None:
    """Improving stint (negative slope, track rubbering in /
    fuel burning off) is at least as good as 0 decay; clamp the
    decay sub-score at 1.0 rather than letting it overshoot.
    Pin: a "magic" 1.2 sub-score would let bad fuel be hidden
    behind impossibly good pace data, masking abuse."""
    score = compute_tire_preservation_score(
        fuel_used_per_lap=MAX_FUEL_PER_LAP * 0.5,  # sub = 0.5
        pace_decay_seconds_per_lap=-0.3,  # negative; clamps to sub=1.0
    )
    assert score is not None
    assert math.isclose(score, 0.5, rel_tol=1e-9)


# -- degenerate inputs --------------------------------------------


def test_negative_fuel_raises() -> None:
    """Fuel use must be >= 0 (a negative value means a parse bug
    or a refuel reading that wasn't filtered). Refuse loudly
    rather than silently producing a >1 score."""
    with pytest.raises(ValueError, match="fuel_used_per_lap"):
        compute_tire_preservation_score(
            fuel_used_per_lap=-1.0,
            pace_decay_seconds_per_lap=0.0,
        )
