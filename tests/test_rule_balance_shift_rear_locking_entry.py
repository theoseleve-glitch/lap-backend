"""T195 — `br.balance_shift.rear_locking_entry` library rule.

Rear axle slips significantly more than front during entry
braking — either brake bias is too rearward or the driver is
loading the front insufficiently before peak deceleration.

Distinct from T192 (raw lockup count): T192 fires on any wheel
locking. T195 fires on a *balance* — rear sliding more than
front, regardless of absolute slip magnitude. A driver can
trigger T195 without ever fully locking up.

Diagnostic-style explanation: suggests both bias adjustment and
weight-transfer technique, since the cause needs human triage.
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
    LIBRARY_DIR / "braking" / "balance_shift_rear_locking_entry.yaml"
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
    assert rule.id == "br.balance_shift.rear_locking_entry"


def test_rule_subcategory(rule: RuleSpec) -> None:
    assert rule.subcategory == "balance_shift"


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
    assert "br.balance_shift.rear_locking_entry" in {
        r.id for r in rules
    }


def test_rule_uses_only_known_feature_names(rule: RuleSpec) -> None:
    compiled = compile_condition(rule.detection.condition)
    require_known_features(compiled.referenced_names)


def test_rule_fires_when_rear_slip_dominates_front(
    rule: RuleSpec,
) -> None:
    """2.0x = rear is slipping twice as much as front. Clear
    balance issue."""
    context = {
        "rear_front_slip_ratio_entry": 2.0,
        "ratio_pct": 200,
        "corner": "Turn 8",
    }
    insights = run_rules([rule], context=context)
    assert len(insights) == 1
    assert (
        insights[0].rule_id
        == "br.balance_shift.rear_locking_entry"
    )


def test_rule_does_not_fire_when_balanced(rule: RuleSpec) -> None:
    """Slip ratio near 1.0 — front and rear sharing the work."""
    context = {
        "rear_front_slip_ratio_entry": 1.05,
        "ratio_pct": 105,
        "corner": "Turn 8",
    }
    assert run_rules([rule], context=context) == []
