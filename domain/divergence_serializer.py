"""T041 (sprint2-backend) — divergence point serialization.

Convert a corner-level CornerDivergence (T039/T040) into per-metric
DivergencePoint records that match the persistence schema
(lap-schemas v1.divergence_point.DivergencePoint).

A CornerDivergence carries a corner-level decision; the schema
stores per-metric rows so charts can render multiple traces. This
module bridges the two by expanding one corner-level decision into
N per-metric rows, sharing the corner's track position and the
corner-level divergence type.

The serializer is pure: no DB writes here. It produces validated
schema objects that a downstream writer (existing bridge endpoint
pipeline) persists.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from lap_schemas.v1.divergence_point import DivergencePoint

from domain.bridge_divergence import CornerDivergence


@dataclass(frozen=True)
class ChannelEvidence:
    metric: str
    sim_value: float
    real_value: float


def serialize_divergence_points(
    corner_divergence: CornerDivergence,
    *,
    track_distance_m: float,
    evidence: Sequence[ChannelEvidence],
) -> list[DivergencePoint]:
    return [
        DivergencePoint(
            track_distance_m=track_distance_m,
            metric=ev.metric,
            sim_value=ev.sim_value,
            real_value=ev.real_value,
            divergence_type=corner_divergence.divergence_type,
        )
        for ev in evidence
    ]
