"""T189 — `br.early_braking.entry_speed_loss` library rule.

Mirror of T188: same telemetry, opposite cause. Driver brakes
too early, scrubs more speed than needed, enters the corner
slow. Symptom is `entry_speed < reference_entry_speed`.

Authoring both rules keeps the braking library symmetric so the
coach can distinguish "you brake too late" from "you brake too
early" — opposite fixes for similar-looking lap times.
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
    LIBRARY_DIR / "braking" / "early_braking_entry_speed_loss.yaml"
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
    assert rule.id == "br.early_braking.entry_speed_loss"


def test_rule_subcategory_is_early_braking(rule: RuleSpec) -> None:
    """Distinguishes from T188's `late_braking` subcategory —
    same parent (br.) but a different symptom and fix."""
    assert rule.subcategory == "early_braking"


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
    assert "br.early_braking.entry_speed_loss" in {
        r.id for r in rules
    }


def test_rule_uses_only_known_feature_names(rule: RuleSpec) -> None:
    compiled = compile_condition(rule.detection.condition)
    require_known_features(compiled.referenced_names)


def test_rule_fires_when_entry_speed_below_reference(
    rule: RuleSpec,
) -> None:
    """Driver entered at 88% of reference — slower than the
    benchmark, classic over-braking symptom."""
    context = {
        "entry_speed": 88.0,
        "reference_entry_speed": 100.0,
        "delta_kmh": 12.0,
        "corner": "Turn 6",
    }
    insights = run_rules([rule], context=context)
    assert len(insights) == 1
    assert insights[0].rule_id == "br.early_braking.entry_speed_loss"


def test_rule_does_not_fire_when_entry_speed_close_to_reference(
    rule: RuleSpec,
) -> None:
    context = {
        "entry_speed": 96.0,
        "reference_entry_speed": 100.0,
        "delta_kmh": 4.0,
        "corner": "Turn 6",
    }
    assert run_rules([rule], context=context) == []
