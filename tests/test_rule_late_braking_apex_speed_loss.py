"""T188 — `br.late_braking.apex_speed_loss` is the first authored
library rule. Lives at
`coaching/library/rules/braking/late_braking_apex_speed_loss.yaml`
so the loader (T182) picks it up at startup.

These tests are the QA gate for the rule itself: does it parse,
does it carry the three difficulty variants ADR-013 requires,
does the condition fire on the canonical "apex speed loss"
scenario, and is its template-variable surface complete?
"""

from pathlib import Path

import pytest
import yaml

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
    LIBRARY_DIR / "braking" / "late_braking_apex_speed_loss.yaml"
)


@pytest.fixture
def rule() -> RuleSpec:
    raw = yaml.safe_load(RULE_PATH.read_text(encoding="utf-8"))
    return RuleSpec.model_validate(raw)


def test_rule_file_exists_at_canonical_library_path() -> None:
    assert RULE_PATH.exists(), (
        f"missing canonical library file: {RULE_PATH}"
    )


def test_rule_id_is_branded_namespace_dot_path(rule: RuleSpec) -> None:
    """Library convention: <category_short>.<subcategory>.<symptom>.
    Avoids ambiguity if two technique categories share a subcategory."""
    assert rule.id == "br.late_braking.apex_speed_loss"


def test_rule_has_three_explanation_variants(rule: RuleSpec) -> None:
    """The whole point of difficulty branching — beginner/
    intermediate/advanced must each have non-empty text."""
    assert rule.explanation_template.beginner.strip()
    assert rule.explanation_template.intermediate.strip()
    assert rule.explanation_template.advanced.strip()


def test_rule_explanations_are_actually_distinct(
    rule: RuleSpec,
) -> None:
    """If all three branches are identical the difficulty selector
    is dead code — the operator forgot to vary phrasing."""
    et = rule.explanation_template
    assert (
        len({et.beginner, et.intermediate, et.advanced}) == 3
    ), "explanation variants must differ"


def test_rule_loads_via_load_rules() -> None:
    """End-to-end: the loader recurses into braking/ and picks
    this rule up cleanly."""
    rules = load_rules(LIBRARY_DIR)
    rule_ids = {r.id for r in rules}
    assert "br.late_braking.apex_speed_loss" in rule_ids


def test_rule_uses_only_known_feature_names(rule: RuleSpec) -> None:
    """T184's namespace gate: every name the condition references
    must be in `FEATURE_NAMESPACE`. A typo (`apex_speeed`) would
    fail here at CI rather than at production startup."""
    from coaching.library.condition_compiler import compile_condition

    compiled = compile_condition(rule.detection.condition)
    require_known_features(compiled.referenced_names)


def test_rule_fires_on_apex_speed_loss_scenario(
    rule: RuleSpec,
) -> None:
    """Driver's apex speed is 92% of reference — under the 0.93
    threshold, so the rule should fire."""
    context = {
        "apex_speed": 86.0,
        "reference_apex_speed": 100.0,
        "delta_kmh": 14.0,
        "corner": "Turn 4",
    }
    insights = run_rules([rule], context=context)
    assert len(insights) == 1
    assert insights[0].rule_id == "br.late_braking.apex_speed_loss"


def test_rule_does_not_fire_when_apex_speed_is_close_to_reference(
    rule: RuleSpec,
) -> None:
    """Driver is at 95% of reference — above the 0.93 threshold."""
    context = {
        "apex_speed": 95.0,
        "reference_apex_speed": 100.0,
        "delta_kmh": 5.0,
        "corner": "Turn 4",
    }
    insights = run_rules([rule], context=context)
    assert insights == []
