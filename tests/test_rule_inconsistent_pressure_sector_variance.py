"""T191 — `br.inconsistent_pressure.sector_variance` library rule.

Different from T188-T190: not about a single corner's mistake,
but about *repeatability* across consecutive laps in the same
sector. Driver applies hard one lap, soft the next — they're
guessing rather than reading the car.

Signal is `brake_pressure_cov_in_sector` (coefficient of
variation = stdev/mean). First rule with no reference benchmark
— consistency is an absolute property; 15% lap-to-lap variation
is sloppy regardless of which corner.
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
    / "inconsistent_pressure_sector_variance.yaml"
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
    assert rule.id == "br.inconsistent_pressure.sector_variance"


def test_rule_subcategory(rule: RuleSpec) -> None:
    assert rule.subcategory == "inconsistent_pressure"


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
    assert "br.inconsistent_pressure.sector_variance" in {
        r.id for r in rules
    }


def test_rule_uses_only_known_feature_names(rule: RuleSpec) -> None:
    """`brake_pressure_cov_in_sector` is new — committed
    alongside this rule's namespace addition."""
    compiled = compile_condition(rule.detection.condition)
    require_known_features(compiled.referenced_names)


def test_rule_fires_on_high_lap_to_lap_variance(
    rule: RuleSpec,
) -> None:
    """0.25 = 25% coefficient of variation — the driver's brake
    pressure is all over the place lap to lap."""
    context = {
        "brake_pressure_cov_in_sector": 0.25,
        "cov_pct": 25,
        "sector": "Sector 2",
    }
    insights = run_rules([rule], context=context)
    assert len(insights) == 1
    assert (
        insights[0].rule_id
        == "br.inconsistent_pressure.sector_variance"
    )


def test_rule_does_not_fire_on_consistent_braking(
    rule: RuleSpec,
) -> None:
    """0.05 = 5% coefficient of variation — well within the
    repeatability band."""
    context = {
        "brake_pressure_cov_in_sector": 0.05,
        "cov_pct": 5,
        "sector": "Sector 2",
    }
    assert run_rules([rule], context=context) == []
