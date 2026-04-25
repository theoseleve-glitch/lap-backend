"""T192 — `br.lockup.pattern_detection` library rule.

Closes the braking block. A lockup happens when wheel speed
drops well below vehicle speed — tires slide rather than roll.
The pipeline (downstream task) detects each lockup; this rule
fires when the *count* in a session crosses a threshold,
indicating a pattern rather than one bad lap.

Discrete count signal — no reference benchmark. Three lockups
in one session is a behavior, not noise.
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
RULE_PATH = LIBRARY_DIR / "braking" / "lockup_pattern_detection.yaml"


@pytest.fixture
def rule() -> RuleSpec:
    raw = yaml.safe_load(RULE_PATH.read_text(encoding="utf-8"))
    return RuleSpec.model_validate(raw)


def test_rule_file_exists() -> None:
    assert RULE_PATH.exists(), (
        f"missing canonical library file: {RULE_PATH}"
    )


def test_rule_id(rule: RuleSpec) -> None:
    assert rule.id == "br.lockup.pattern_detection"


def test_rule_subcategory(rule: RuleSpec) -> None:
    assert rule.subcategory == "lockup"


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
    assert "br.lockup.pattern_detection" in {r.id for r in rules}


def test_rule_uses_only_known_feature_names(rule: RuleSpec) -> None:
    """`lockup_event_count` is new — committed alongside the
    namespace addition."""
    compiled = compile_condition(rule.detection.condition)
    require_known_features(compiled.referenced_names)


def test_rule_fires_on_three_or_more_lockups(rule: RuleSpec) -> None:
    """3 lockups = pattern, not luck."""
    context = {
        "lockup_event_count": 5,
        "count": 5,
    }
    insights = run_rules([rule], context=context)
    assert len(insights) == 1
    assert insights[0].rule_id == "br.lockup.pattern_detection"


def test_rule_does_not_fire_on_a_single_lockup(rule: RuleSpec) -> None:
    """One lockup is bad luck — could be cold tires, debris,
    a single overdriven corner. Not a coachable pattern yet."""
    context = {"lockup_event_count": 1, "count": 1}
    assert run_rules([rule], context=context) == []
