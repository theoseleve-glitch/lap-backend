"""T194 — `br.over_braking.mid_corner` library rule.

Driver carries meaningful brake pressure past the apex when the
brake pedal should be near zero (hand-off to throttle). Signal
is `mid_corner_brake_pressure_pct` — apex-region brake pressure
as a fraction of peak entry pressure. Fires when > 0.20: 20%
residual at apex means the driver isn't trail-braking off, they're
still scrubbing speed when the car should be carrying through.

Distinct from T188 (apex-speed loss). T188 is the outcome,
T194 is one cause. A driver can have correct apex speed AND
trigger T194 — over-slowed on entry, compensating with mid-corner
brake. Two rules, two diagnoses, two drills.
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
RULE_PATH = LIBRARY_DIR / "braking" / "over_braking_mid_corner.yaml"


@pytest.fixture
def rule() -> RuleSpec:
    raw = yaml.safe_load(RULE_PATH.read_text(encoding="utf-8"))
    return RuleSpec.model_validate(raw)


def test_rule_file_exists() -> None:
    assert RULE_PATH.exists(), (
        f"missing canonical library file: {RULE_PATH}"
    )


def test_rule_id(rule: RuleSpec) -> None:
    assert rule.id == "br.over_braking.mid_corner"


def test_rule_subcategory(rule: RuleSpec) -> None:
    assert rule.subcategory == "over_braking"


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
    assert "br.over_braking.mid_corner" in {r.id for r in rules}


def test_rule_uses_only_known_feature_names(rule: RuleSpec) -> None:
    compiled = compile_condition(rule.detection.condition)
    require_known_features(compiled.referenced_names)


def test_rule_fires_when_brake_pressure_lingers_past_apex(
    rule: RuleSpec,
) -> None:
    """30% residual at apex — driver is still scrubbing speed."""
    context = {
        "mid_corner_brake_pressure_pct": 0.30,
        "residual_pct": 30,
        "corner": "Turn 5",
    }
    insights = run_rules([rule], context=context)
    assert len(insights) == 1
    assert insights[0].rule_id == "br.over_braking.mid_corner"


def test_rule_does_not_fire_when_brake_is_off_at_apex(
    rule: RuleSpec,
) -> None:
    """5% residual — within the trail-brake taper-off range."""
    context = {
        "mid_corner_brake_pressure_pct": 0.05,
        "residual_pct": 5,
        "corner": "Turn 5",
    }
    assert run_rules([rule], context=context) == []
