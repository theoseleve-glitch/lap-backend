"""T184 — feature namespace for coaching rules.

The compiler (T183) validates that an expression is *syntactically*
safe but can't tell a real feature name (`apex_speed`) from a
typo (`apex_speeed`). The namespace is the second check: at rule
load time, every name a rule references must be one we know how
to provide. A typo fails at startup, not silently at runtime.
"""

import pytest

from coaching.library.features import (
    FEATURE_NAMESPACE,
    FEATURES,
    FeatureKind,
    UnknownFeaturesError,
    require_known_features,
)


def test_namespace_is_a_non_empty_set_of_strings() -> None:
    assert isinstance(FEATURE_NAMESPACE, frozenset)
    assert len(FEATURE_NAMESPACE) > 0
    assert all(isinstance(n, str) for n in FEATURE_NAMESPACE)


def test_namespace_includes_adr_013_canonical_features() -> None:
    """ADR-013 names two features explicitly in its example. They
    are part of the locked contract — must be in the namespace."""
    assert "apex_speed" in FEATURE_NAMESPACE
    assert "reference_apex_speed" in FEATURE_NAMESPACE


def test_namespace_includes_existing_session_metrics() -> None:
    """`SessionMetrics` (T119) already produces these. The runner
    will plumb them through to conditions."""
    for name in (
        "best_lap_ms",
        "avg_lap_ms",
        "consistency_ms",
        "peak_speed_kph",
    ):
        assert name in FEATURE_NAMESPACE, f"{name} missing"


def test_features_metadata_covers_namespace() -> None:
    """Every name in the namespace must have a `FEATURES` entry —
    the metadata is the docs site's source for what each feature
    means. If a name has no entry, the docs are silently
    incomplete."""
    assert set(FEATURES.keys()) == set(FEATURE_NAMESPACE)


def test_each_feature_has_unit_and_description() -> None:
    for name, spec in FEATURES.items():
        assert spec.unit, f"{name} missing unit"
        assert spec.description, f"{name} missing description"


def test_features_have_a_kind_distinguishing_pipeline_vs_reference() -> None:
    """The runner needs to know whether to compute from the current
    session or fetch from a benchmark store."""
    pipeline_features = [
        n for n, s in FEATURES.items() if s.kind == FeatureKind.PIPELINE
    ]
    reference_features = [
        n for n, s in FEATURES.items() if s.kind == FeatureKind.REFERENCE
    ]
    assert "apex_speed" in pipeline_features
    assert "reference_apex_speed" in reference_features


def test_require_known_features_passes_for_known_names() -> None:
    require_known_features(
        {"apex_speed", "reference_apex_speed"}
    )


def test_require_known_features_raises_with_unknown_names() -> None:
    with pytest.raises(UnknownFeaturesError) as exc_info:
        require_known_features({"apex_speed", "apex_speeed"})
    assert "apex_speeed" in str(exc_info.value)


def test_require_known_features_lists_all_unknowns() -> None:
    """Operator should see every typo at once, not have to fix one
    rule at a time across multiple startup attempts."""
    with pytest.raises(UnknownFeaturesError) as exc_info:
        require_known_features({"foo", "bar", "apex_speed"})
    msg = str(exc_info.value)
    assert "foo" in msg
    assert "bar" in msg
