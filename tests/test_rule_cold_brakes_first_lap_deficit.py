"""T193 — `br.cold_brakes.first_lap_deficit` library rule.

Diagnoses a *physical* condition rather than a driver error:
cold brake discs/pads can't generate the friction warm ones do,
so lap 1 braking distances are longer than later laps. The rule
fires when first-lap brake distance is ≥10% longer than the
reference (driver's warm-brake baseline).

Tone of the explanation matters here: this isn't "you're doing
it wrong" — it's "expect this and plan around it" (build pad
heat in the out-lap, leave more margin into corners). Coaching
language that frames physics as technique failure would be
both wrong and condescending.
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
    LIBRARY_DIR / "braking" / "cold_brakes_first_lap_deficit.yaml"
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
    assert rule.id == "br.cold_brakes.first_lap_deficit"


def test_rule_subcategory(rule: RuleSpec) -> None:
    assert rule.subcategory == "cold_brakes"


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
    assert "br.cold_brakes.first_lap_deficit" in {
        r.id for r in rules
    }


def test_rule_uses_only_known_feature_names(rule: RuleSpec) -> None:
    compiled = compile_condition(rule.detection.condition)
    require_known_features(compiled.referenced_names)


def test_rule_fires_when_first_lap_brake_distance_is_long(
    rule: RuleSpec,
) -> None:
    """1.18 = 18% longer braking on lap 1 vs reference — clear
    cold-brake symptom."""
    context = {
        "first_lap_brake_distance_ratio": 1.18,
        "deficit_pct": 18,
    }
    insights = run_rules([rule], context=context)
    assert len(insights) == 1
    assert insights[0].rule_id == "br.cold_brakes.first_lap_deficit"


def test_rule_does_not_fire_when_first_lap_is_at_reference_pace(
    rule: RuleSpec,
) -> None:
    """1.05 = 5% — within normal lap-to-lap variance, not a
    pattern worth surfacing."""
    context = {
        "first_lap_brake_distance_ratio": 1.05,
        "deficit_pct": 5,
    }
    assert run_rules([rule], context=context) == []
