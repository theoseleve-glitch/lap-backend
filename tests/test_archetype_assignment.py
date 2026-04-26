"""T060 (sprint2-backend) — archetype assignment.

Given a driver's per-dimension percentiles (output of T058's
benchmark endpoint), tag them with one of five archetypes
defined in lap_schemas.v1.driver_benchmark_profile. Rule-based,
not ML — the dimensions map to archetypes in a fixed table:

  late_braker         → braking
  aggressive_exit     → throttle_pickup
  smooth_flow         → line_consistency + minimum_speed (avg)
  consistent_executor → fatigue_consistency
  adaptive_racecraft  → adaptation + racecraft (avg)

Each archetype's score is the mean of its mapped dimensions'
percentiles; the highest-scoring archetype wins IF that score
clears `ARCHETYPE_THRESHOLD` (0.7 = top 30%). Below threshold
returns None (the driver is not strongly any archetype yet).
"""

from __future__ import annotations

from domain.archetype import (
    ARCHETYPE_THRESHOLD,
    assign_archetype,
)


def test_high_braking_assigns_late_braker() -> None:
    """Top-quartile braking → late_braker. Pin the dimension
    mapping so a future "moved braking to a different archetype"
    refactor trips this test."""
    percentiles = {
        "braking": 0.9, "throttle_pickup": 0.4,
        "line_consistency": 0.3, "minimum_speed": 0.3,
        "fatigue_consistency": 0.4, "adaptation": 0.3,
        "racecraft": 0.3,
    }
    assert assign_archetype(percentiles) == "late_braker"


def test_high_throttle_pickup_assigns_aggressive_exit() -> None:
    """Top-quartile throttle_pickup → aggressive_exit."""
    percentiles = {
        "braking": 0.4, "throttle_pickup": 0.85,
        "line_consistency": 0.3, "minimum_speed": 0.3,
        "fatigue_consistency": 0.3,
    }
    assert assign_archetype(percentiles) == "aggressive_exit"


def test_high_line_consistency_and_min_speed_assigns_smooth_flow() -> None:
    """smooth_flow needs BOTH line_consistency and minimum_speed
    to average above threshold — neither alone suffices because
    the archetype is the avg of both."""
    percentiles = {
        "braking": 0.4, "throttle_pickup": 0.4,
        "line_consistency": 0.8, "minimum_speed": 0.75,
        "fatigue_consistency": 0.4,
    }
    assert assign_archetype(percentiles) == "smooth_flow"


def test_high_fatigue_consistency_assigns_consistent_executor() -> None:
    """Top-quartile fatigue_consistency → consistent_executor.
    The "doesn't degrade over a stint" archetype."""
    percentiles = {
        "braking": 0.4, "throttle_pickup": 0.4,
        "line_consistency": 0.4, "minimum_speed": 0.3,
        "fatigue_consistency": 0.9,
    }
    assert assign_archetype(percentiles) == "consistent_executor"


def test_high_adaptation_and_racecraft_assigns_adaptive_racecraft() -> None:
    """adaptive_racecraft = avg(adaptation, racecraft) above threshold."""
    percentiles = {
        "braking": 0.4, "throttle_pickup": 0.4,
        "line_consistency": 0.3, "minimum_speed": 0.3,
        "fatigue_consistency": 0.4,
        "adaptation": 0.85, "racecraft": 0.8,
    }
    assert assign_archetype(percentiles) == "adaptive_racecraft"


def test_all_below_threshold_returns_none() -> None:
    """A driver with no top-quartile dimensions gets no archetype
    — null, not "default to first". The UI shows "no archetype
    yet" rather than a misleading tag."""
    percentiles = {
        "braking": 0.5, "throttle_pickup": 0.4,
        "line_consistency": 0.5, "minimum_speed": 0.5,
        "fatigue_consistency": 0.5, "adaptation": 0.5,
        "racecraft": 0.5,
    }
    assert assign_archetype(percentiles) is None


def test_empty_percentiles_returns_none() -> None:
    """No data → no archetype. Edge case for cohort-locked drivers
    whose benchmark endpoint returned percentiles=null."""
    assert assign_archetype({}) is None


def test_threshold_inclusive() -> None:
    """Exactly at ARCHETYPE_THRESHOLD → archetype assigned (>=,
    not >). Pin inclusivity so a future tightening to > is
    explicit."""
    percentiles = {
        "braking": ARCHETYPE_THRESHOLD,
        "throttle_pickup": 0.0, "line_consistency": 0.0,
        "minimum_speed": 0.0, "fatigue_consistency": 0.0,
    }
    assert assign_archetype(percentiles) == "late_braker"


def test_smooth_flow_requires_both_dims_to_average_above_threshold() -> None:
    """Edge case for multi-dim archetypes: line_consistency=0.9
    but minimum_speed=0.4 → avg=0.65 → below 0.7. Should NOT
    tag smooth_flow even though one dim is high. Falls through
    to None because no archetype clears threshold."""
    percentiles = {
        "braking": 0.4, "throttle_pickup": 0.4,
        "line_consistency": 0.9, "minimum_speed": 0.4,
        "fatigue_consistency": 0.4,
    }
    assert assign_archetype(percentiles) is None


def test_highest_scoring_archetype_wins_over_others() -> None:
    """Two archetypes both above threshold — the higher one
    wins. Pin the tie-break / arg-max behavior."""
    percentiles = {
        "braking": 0.95,         # late_braker = 0.95
        "throttle_pickup": 0.80, # aggressive_exit = 0.80
        "line_consistency": 0.4, "minimum_speed": 0.4,
        "fatigue_consistency": 0.4,
    }
    assert assign_archetype(percentiles) == "late_braker"
