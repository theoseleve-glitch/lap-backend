"""T039 (sprint2-backend) — divergence detection (corner-level).

Given per-corner aggregate scores from T034 (shape) and T037
(intensity), classify which corners exhibit a known divergence
pattern. T039 implements the "intensity" pattern; T040 will add
"shape"; T041 will enrich corner-level CornerDivergences into the
per-channel DivergencePoint records the schema persists.

Divergence types align with the schema's
DivergencePoint.divergence_type Literal:

    "intensity" — shape ≥ SHAPE_HIGH and intensity ≤ INTENSITY_LOW
                  ("right thing, wrong magnitude")
    "shape"     — shape ≤ SHAPE_LOW (T040)
    "both"      — reserved for combined cases (T040+)

Thresholds are inclusive on the failing side (≥, ≤), matching the
sprint wording literally.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

SHAPE_HIGH = 0.9
INTENSITY_LOW = 0.6

DivergenceType = Literal["shape", "intensity", "both"]


@dataclass(frozen=True)
class CornerScore:
    corner_name: str
    shape_score: float
    intensity_score: float


@dataclass(frozen=True)
class CornerDivergence:
    corner_name: str
    divergence_type: DivergenceType
    shape_score: float
    intensity_score: float


def detect_intensity_divergences(
    per_corner: Sequence[CornerScore],
) -> list[CornerDivergence]:
    return [
        CornerDivergence(
            corner_name=cs.corner_name,
            divergence_type="intensity",
            shape_score=cs.shape_score,
            intensity_score=cs.intensity_score,
        )
        for cs in per_corner
        if cs.shape_score >= SHAPE_HIGH
        and cs.intensity_score <= INTENSITY_LOW
    ]
