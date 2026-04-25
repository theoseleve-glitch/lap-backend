"""T196 — `br.trail_brake.release_too_early` library rule.

Driver releases the brake well before the apex, missing the
chassis-loading benefit of trail-braking. The front goes light
through rotation, the car understeers or "pushes" mid-corner.

Symmetric pair with T194 (over-braking past apex):
  - T194: brake on too long  (release_position_pct ~ 110%)
  - T196: brake off too soon (release_position_pct < 60%)
Together they bound the optimal trail-brake-release window.

Signal is `brake_release_position_pct`: 0% = released at the
braking marker (way too early), 100% = released exactly at apex
(target). Fires when < 60%.
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
    LIBRARY_DIR / "braking" / "trail_brake_release_too_early.yaml"
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
    assert rule.id == "br.trail_brake.release_too_early"


def test_rule_subcategory(rule: RuleSpec) -> None:
    assert rule.subcategory == "trail_brake"


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
    assert "br.trail_brake.release_too_early" in {
        r.id for r in rules
    }


def test_rule_uses_only_known_feature_names(rule: RuleSpec) -> None:
    compiled = compile_condition(rule.detection.condition)
    require_known_features(compiled.referenced_names)


def test_rule_fires_when_brake_released_well_before_apex(
    rule: RuleSpec,
) -> None:
    """45% of the way to the apex — brake fully off, half the
    corner still ahead."""
    context = {
        "brake_release_position_pct": 0.45,
        "release_pct": 45,
        "corner": "Turn 2",
    }
    insights = run_rules([rule], context=context)
    assert len(insights) == 1
    assert insights[0].rule_id == "br.trail_brake.release_too_early"


def test_rule_does_not_fire_when_brake_held_into_apex(
    rule: RuleSpec,
) -> None:
    """85% release point — brake comes off near the apex,
    proper trail-brake handoff."""
    context = {
        "brake_release_position_pct": 0.85,
        "release_pct": 85,
        "corner": "Turn 2",
    }
    assert run_rules([rule], context=context) == []
