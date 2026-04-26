"""T039 (sprint2-backend) — divergence detection: intensity type.

When a corner's shape match is high (≥0.9) but its intensity match
is low (≤0.6), that's "same shape, different intensity" — the
driver did the right *thing* with the *wrong* magnitude. Pin the
thresholds and the corner-level scan here.

T040 will add the "shape" type (low shape regardless of intensity);
T041 will enrich each detection with per-channel sim/real values
to satisfy the DivergencePoint persistence schema. This task is
just the corner-level classifier.

Thresholds are inclusive on the failing side (≥0.9, ≤0.6) — a
boundary score still counts as the pattern. Picked to match the
sprint description literally; if a future ADR loosens them we
catch it here.
"""

from __future__ import annotations

from domain.bridge_divergence import (
    CornerScore,
    detect_intensity_divergences,
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
