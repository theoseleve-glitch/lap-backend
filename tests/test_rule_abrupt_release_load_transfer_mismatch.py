"""T190 — `br.abrupt_release.load_transfer_mismatch` library rule.

Different mechanism from T188/T189. Same telemetry symptoms can
come from a brake-release rate too high — driver "stomps off"
the pedal, weight transfer snaps forward, front tires unload at
the moment they need grip for turn-in. Detected via
`brake_release_rate > reference * 1.5` (note: opposite direction
from T188/T189 — *above* reference is the failure here).

This is the first rule that reads a derivative signal rather
than a snapshot, so the namespace gains
`brake_release_rate` / `reference_brake_release_rate`.
"""

from pathlib import Path

import pytest
import yaml

from coaching.library.condition_compiler import compile_condition
from coaching.library.features import require_known_features
from coaching.library.rule_loader import load_rules
from coaching.library.rule_runner import run_rules
from coaching.library.rule_schema import RuleSpec

LIBRARY_DIR = (
    Path(__file__).resolve().parents[1]
    / "coaching"
    / "library"
    / "rules"
)
RULE_PATH = (
    LIBRARY_DIR
    / "braking"
    / "abrupt_release_load_transfer_mismatch.yaml"
)


@pytest.fixture
def rule() -> RuleSpec:
    raw = yaml.safe_load(RULE_PATH.read_text(encoding="utf-8"))
    return RuleSpec.model_validate(raw)


def test_rule_file_exists() -> None:
    assert RULE_PATH.exists(), (
        f"missing canonical library file: {RULE_PATH}"
    )


def test_rule_id(rule: RuleSpec) -> None:
    assert rule.id == "br.abrupt_release.load_transfer_mismatch"


def test_rule_subcategory_is_abrupt_release(rule: RuleSpec) -> None:
    assert rule.subcategory == "abrupt_release"


def test_rule_has_three_distinct_explanation_variants(
    rule: RuleSpec,
) -> None:
    et = rule.explanation_template
    assert et.beginner.strip()
    assert et.intermediate.strip()
    assert et.advanced.strip()
    assert len({et.beginner, et.intermediate, et.advanced}) == 3


def test_rule_loads_via_load_rules() -> None:
    rules = load_rules(LIBRARY_DIR)
    assert "br.abrupt_release.load_transfer_mismatch" in {
        r.id for r in rules
    }


def test_rule_uses_only_known_feature_names(rule: RuleSpec) -> None:
    """`brake_release_rate` is new to T184 — committed alongside
    this rule. The QA gate verifies it landed."""
    compiled = compile_condition(rule.detection.condition)
    require_known_features(compiled.referenced_names)


def test_rule_fires_when_release_rate_exceeds_reference(
    rule: RuleSpec,
) -> None:
    """Driver releases at 2.5x reference — abrupt, weight snaps
    forward, classic load-transfer mismatch."""
    context = {
        "brake_release_rate": 5.0,
        "reference_brake_release_rate": 2.0,
        "delta_pct": 150.0,
        "corner": "Turn 9",
    }
    insights = run_rules([rule], context=context)
    assert len(insights) == 1
    assert (
        insights[0].rule_id
        == "br.abrupt_release.load_transfer_mismatch"
    )


def test_rule_does_not_fire_when_release_is_smooth(
    rule: RuleSpec,
) -> None:
    """Release at 1.1x reference — within the smoothness band."""
    context = {
        "brake_release_rate": 2.2,
        "reference_brake_release_rate": 2.0,
        "delta_pct": 10.0,
        "corner": "Turn 9",
    }
    assert run_rules([rule], context=context) == []
