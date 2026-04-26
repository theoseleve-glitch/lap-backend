"""T060 (sprint2-backend) — driver archetype assignment.

Rule-based mapping from per-dimension percentiles → one of the
five DriverArchetype tags (see lap_schemas.v1.driver_benchmark_profile).

Each archetype is associated with one or more skill dimensions;
its "score" is the mean of those dimensions' percentile ranks.
The highest-scoring archetype wins iff its score clears
ARCHETYPE_THRESHOLD (top 30%); below the threshold the driver
gets no tag (None) rather than a misleading default.

Why threshold and not always-pick-best: percentiles cluster around
0.5 for an average driver. Without a floor, every driver would be
tagged with whichever archetype happens to be marginally above
their other dims — meaningless. Requiring >=0.7 means "you're
genuinely strong in this area" before we attach a label.

Multi-dim archetypes (smooth_flow, adaptive_racecraft) require
the AVERAGE of their dims to clear threshold, not just one dim
spiking. A driver with line_consistency=0.9 but minimum_speed=0.4
isn't "smooth flow" — they're inconsistent under different
contexts.
"""

from __future__ import annotations

from typing import Literal

DriverArchetype = Literal[
    "late_braker",
    "smooth_flow",
    "aggressive_exit",
    "consistent_executor",
    "adaptive_racecraft",
]

ARCHETYPE_THRESHOLD = 0.7

# Mapping from archetype tag → tuple of skill dimensions whose
# percentiles average together to score that archetype. Order of
# tuples does not matter; mean is order-invariant.
_ARCHETYPE_DIMENSIONS: dict[DriverArchetype, tuple[str, ...]] = {
    "late_braker": ("braking",),
    "aggressive_exit": ("throttle_pickup",),
    "smooth_flow": ("line_consistency", "minimum_speed"),
    "consistent_executor": ("fatigue_consistency",),
    "adaptive_racecraft": ("adaptation", "racecraft"),
}


def assign_archetype(
    percentiles: dict[str, float],
) -> DriverArchetype | None:
    """Return the strongest archetype, or None if none clears threshold.

    `percentiles` keys are skill dimension names; values are
    [0, 1] percentile ranks (output of T058). Missing dimensions
    are skipped; an archetype whose required dims are all missing
    scores nothing and is excluded.
    """
    scores: dict[DriverArchetype, float] = {}
    for archetype, dims in _ARCHETYPE_DIMENSIONS.items():
        present = [percentiles[d] for d in dims if d in percentiles]
        if len(present) != len(dims):
            # Multi-dim archetype with one dim missing → skip
            # (don't reward partial-data archetypes).
            continue
        scores[archetype] = sum(present) / len(present)

    if not scores:
        return None
    best, best_score = max(scores.items(), key=lambda kv: kv[1])
    if best_score < ARCHETYPE_THRESHOLD:
        return None
    return best
