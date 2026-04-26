"""T039 + T040 (sprint2-backend) — divergence detection (corner-level).

Given per-corner aggregate scores from T034 (shape) and T037
(intensity), classify which corners exhibit a known divergence
pattern. T041 will enrich corner-level CornerDivergences into the
per-channel DivergencePoint records the schema persists.

Divergence types align with the schema's
DivergencePoint.divergence_type Literal:

    "intensity" — shape ≥ SHAPE_HIGH and intensity ≤ INTENSITY_LOW
                  ("right thing, wrong magnitude") — T039
    "shape"     — shape ≤ SHAPE_LOW; intensity is moot once
                  technique diverges — T040
    "both"      — reserved for future combined-classification work

Thresholds are inclusive on the failing side (≥, ≤), matching the
sprint wording literally. T039 and T040 are disjoint by construction
(shape ≥0.9 vs shape ≤0.5 — no corner trips both).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

SHAPE_HIGH = 0.9
SHAPE_LOW = 0.5
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


def detect_shape_divergences(
    per_corner: Sequence[CornerScore],
) -> list[CornerDivergence]:
    return [
        CornerDivergence(
            corner_name=cs.corner_name,
            divergence_type="shape",
            shape_score=cs.shape_score,
            intensity_score=cs.intensity_score,
        )
        for cs in per_corner
        if cs.shape_score <= SHAPE_LOW
    ]
