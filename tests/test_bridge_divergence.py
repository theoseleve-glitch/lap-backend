"""T039 + T040 (sprint2-backend) — divergence detection.

T039 — "intensity" type: shape ≥0.9 AND intensity ≤0.6. The driver
       did the right *thing* with the *wrong* magnitude.
T040 — "shape" type: shape ≤0.5. The shapes are too different to
       call it the same technique — "different technique"
       divergence in sprint terms.

T041 will enrich corner-level CornerDivergences into per-channel
DivergencePoint records that match the persistence schema. These
tasks are just the corner-level classifiers.

Thresholds are inclusive on the failing side (≥0.9, ≤0.6, ≤0.5) —
a boundary score still counts as the pattern. Picked to match the
sprint wording literally; if a future ADR loosens them we catch
it here.
"""

from __future__ import annotations

from domain.bridge_divergence import (
    CornerScore,
    detect_intensity_divergences,
    detect_shape_divergences,
)


def _scored(
    corner: str, *, shape: float, intensity: float
) -> CornerScore:
    return CornerScore(
        corner_name=corner, shape_score=shape, intensity_score=intensity
    )


# -- positive matches ---------------------------------------------


def test_high_shape_low_intensity_emits_divergence() -> None:
    """Canonical case: shape 0.95, intensity 0.4 → 'intensity'
    divergence flagged for that corner."""
    cs = [_scored("T1", shape=0.95, intensity=0.4)]
    out = detect_intensity_divergences(cs)
    assert len(out) == 1
    assert out[0].corner_name == "T1"
    assert out[0].divergence_type == "intensity"


def test_returned_divergence_carries_scores() -> None:
    """The detected divergence preserves the input scores so T041
    can serialize them without re-running the score functions."""
    cs = [_scored("T1", shape=0.95, intensity=0.4)]
    out = detect_intensity_divergences(cs)
    assert out[0].shape_score == 0.95
    assert out[0].intensity_score == 0.4


def test_thresholds_inclusive_at_boundary() -> None:
    """shape == 0.9 and intensity == 0.6 still match — inclusive
    on both bounds. Picked to match the sprint wording literally
    (≥, ≤)."""
    cs = [_scored("T1", shape=0.9, intensity=0.6)]
    out = detect_intensity_divergences(cs)
    assert len(out) == 1


# -- negative cases -----------------------------------------------


def test_high_shape_normal_intensity_no_divergence() -> None:
    """Driver did the right thing with the right magnitude — no
    divergence to flag."""
    cs = [_scored("T1", shape=0.95, intensity=0.85)]
    assert detect_intensity_divergences(cs) == []


def test_low_shape_low_intensity_no_intensity_divergence() -> None:
    """Low shape is T040's territory. T039 only fires when shape
    is high — different technique gets handled separately, not
    re-classified as intensity."""
    cs = [_scored("T1", shape=0.4, intensity=0.4)]
    assert detect_intensity_divergences(cs) == []


def test_just_below_shape_threshold_excluded() -> None:
    """0.89 < 0.9 → not high enough; the moderate-shape band is
    *not* an intensity divergence."""
    cs = [_scored("T1", shape=0.89, intensity=0.4)]
    assert detect_intensity_divergences(cs) == []


def test_just_above_intensity_threshold_excluded() -> None:
    """0.61 > 0.6 → intensity not low enough; not a divergence."""
    cs = [_scored("T1", shape=0.95, intensity=0.61)]
    assert detect_intensity_divergences(cs) == []


def test_empty_input_returns_empty() -> None:
    assert detect_intensity_divergences([]) == []


# -- multi-corner -------------------------------------------------


def test_only_matching_corners_returned_in_order() -> None:
    """When some corners match and others don't, only the matches
    come back, in input order — caller can rely on stable order."""
    cs = [
        _scored("T1", shape=0.95, intensity=0.4),  # match
        _scored("T2", shape=0.95, intensity=0.95),  # no
        _scored("T3", shape=0.4, intensity=0.4),  # no (T040)
        _scored("T4", shape=0.92, intensity=0.5),  # match
    ]
    out = detect_intensity_divergences(cs)
    assert [d.corner_name for d in out] == ["T1", "T4"]


# -- T040: "shape" type ("different technique") -------------------


def test_low_shape_emits_shape_divergence() -> None:
    """Canonical case: shape 0.3 → 'shape' divergence, type label
    matches the schema's Literal ('shape')."""
    cs = [_scored("T1", shape=0.3, intensity=0.9)]
    out = detect_shape_divergences(cs)
    assert len(out) == 1
    assert out[0].corner_name == "T1"
    assert out[0].divergence_type == "shape"


def test_shape_divergence_independent_of_intensity() -> None:
    """T040 fires on shape alone — high or low intensity doesn't
    block the classification. The shapes being too different is
    the whole point; intensity is moot once technique diverges."""
    high_intensity = [_scored("T1", shape=0.4, intensity=0.95)]
    low_intensity = [_scored("T1", shape=0.4, intensity=0.1)]
    assert len(detect_shape_divergences(high_intensity)) == 1
    assert len(detect_shape_divergences(low_intensity)) == 1


def test_shape_divergence_threshold_inclusive_at_0_5() -> None:
    """shape == 0.5 still matches — boundary is inclusive, matching
    the sprint wording (≤0.5)."""
    cs = [_scored("T1", shape=0.5, intensity=0.5)]
    out = detect_shape_divergences(cs)
    assert len(out) == 1


def test_shape_divergence_just_above_threshold_excluded() -> None:
    """0.51 > 0.5 → not low enough; the moderate-shape band is
    *not* a 'shape' divergence (it's the no-classification band
    between T039 and T040)."""
    cs = [_scored("T1", shape=0.51, intensity=0.5)]
    assert detect_shape_divergences(cs) == []


def test_shape_divergence_high_shape_no_divergence() -> None:
    """T040 only fires when shape is low — high-shape corners are
    handled by T039 if anywhere."""
    cs = [_scored("T1", shape=0.95, intensity=0.4)]
    assert detect_shape_divergences(cs) == []


def test_shape_divergence_empty_input_returns_empty() -> None:
    assert detect_shape_divergences([]) == []


def test_shape_divergence_carries_scores() -> None:
    """The detected divergence preserves both scores — T041 can
    serialize them without re-running the score functions."""
    cs = [_scored("T1", shape=0.4, intensity=0.7)]
    out = detect_shape_divergences(cs)
    assert out[0].shape_score == 0.4
    assert out[0].intensity_score == 0.7


def test_shape_divergence_filters_in_order() -> None:
    cs = [
        _scored("T1", shape=0.4, intensity=0.5),  # match
        _scored("T2", shape=0.95, intensity=0.95),  # no
        _scored("T3", shape=0.5, intensity=0.5),  # match (boundary)
        _scored("T4", shape=0.7, intensity=0.5),  # no (mid band)
    ]
    out = detect_shape_divergences(cs)
    assert [d.corner_name for d in out] == ["T1", "T3"]


def test_shape_and_intensity_detectors_are_disjoint() -> None:
    """A corner can't trip both detectors at once: T039 needs shape
    ≥0.9, T040 needs shape ≤0.5. Sanity check that the threshold
    choices keep them mutually exclusive."""
    corners = [
        _scored("T1", shape=0.4, intensity=0.4),  # T040 only
        _scored("T2", shape=0.95, intensity=0.4),  # T039 only
        _scored("T3", shape=0.7, intensity=0.4),  # neither
    ]
    intensity = {d.corner_name for d in detect_intensity_divergences(corners)}
    shape = {d.corner_name for d in detect_shape_divergences(corners)}
    assert intensity.isdisjoint(shape)
    assert intensity == {"T2"}
    assert shape == {"T1"}
