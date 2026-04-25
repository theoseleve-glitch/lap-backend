"""T197 — `br.threshold_braking.not_reached` library rule.

Driver isn't pushing brake pedal hard enough to use the tires'
available grip. Peak deceleration sits well below the
theoretical max — leaving braking distance on the table at
every corner.

Symmetric pair with T192 (lockup):
  - T192: too much pedal, exceeded grip
  - T197: too little pedal, didn't reach grip
Together they bracket the optimal-pressure window.

Closes the 10-rule braking block (T188-T197).
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
    LIBRARY_DIR / "braking" / "threshold_braking_not_reached.yaml"
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
    assert rule.id == "br.threshold_braking.not_reached"


def test_rule_subcategory(rule: RuleSpec) -> None:
    assert rule.subcategory == "threshold_braking"


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
    assert "br.threshold_braking.not_reached" in {
        r.id for r in rules
    }


def test_rule_uses_only_known_feature_names(rule: RuleSpec) -> None:
    compiled = compile_condition(rule.detection.condition)
    require_known_features(compiled.referenced_names)


def test_rule_fires_when_peak_deceleration_is_well_below_grip(
    rule: RuleSpec,
) -> None:
    """0.75 = using only 75% of available grip — clear
    under-driven braking."""
    context = {
        "peak_decel_grip_ratio": 0.75,
        "usage_pct": 75,
    }
    insights = run_rules([rule], context=context)
    assert len(insights) == 1
    assert insights[0].rule_id == "br.threshold_braking.not_reached"


def test_rule_does_not_fire_when_close_to_threshold(
    rule: RuleSpec,
) -> None:
    """0.93 = 93% of available grip — within the realistic
    threshold-braking band."""
    context = {
        "peak_decel_grip_ratio": 0.93,
        "usage_pct": 93,
    }
    assert run_rules([rule], context=context) == []
