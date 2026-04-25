"""T186 — emitted insights carry the rule's version.

The schema (T181) already requires `version: int` on every rule.
This test pins the propagation: when the runner fires a rule, the
resulting `CoachingInsight` carries `rule_version` so downstream
analytics, dashboards, and invalidation queries can attribute the
insight to a specific rule definition.

Without the version, "did v1 of this rule ever produce this
insight?" is unanswerable once the rule has been rewritten.
"""

from pathlib import Path
from typing import Any

import yaml

from coaching.library.rule_runner import run_rules
from coaching.library.rule_schema import RuleSpec

ADR_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "coaching"
    / "adr_013_example.yaml"
)


def _load_rule_with_version(version: int) -> RuleSpec:
    raw: dict[str, Any] = yaml.safe_load(
        ADR_FIXTURE.read_text(encoding="utf-8")
    )
    raw["version"] = version
    return RuleSpec.model_validate(raw)


def _firing_context() -> dict[str, Any]:
    return {
        "apex_speed": 85.0,
        "reference_apex_speed": 100.0,
        "delta_kmh": 15.0,
        "corner": "Turn 1",
    }


def test_insight_has_rule_version_field() -> None:
    rule = _load_rule_with_version(1)
    [insight] = run_rules([rule], context=_firing_context())
    assert insight.rule_version == 1


def test_insight_carries_rules_actual_version() -> None:
    rule = _load_rule_with_version(7)
    [insight] = run_rules([rule], context=_firing_context())
    assert insight.rule_version == 7


def test_two_versions_of_same_rule_produce_independently_versioned_insights() -> None:
    """If two versions of br.X.y are loaded (e.g., during a
    canary), both fire and each insight tags its own version."""
    raw_v1: dict[str, Any] = yaml.safe_load(
        ADR_FIXTURE.read_text(encoding="utf-8")
    )
    raw_v1["version"] = 1
    raw_v1["id"] = "br.canary.v1"
    raw_v2 = dict(raw_v1)
    raw_v2["version"] = 2
    raw_v2["id"] = "br.canary.v2"

    rules = [
        RuleSpec.model_validate(raw_v1),
        RuleSpec.model_validate(raw_v2),
    ]
    insights = run_rules(rules, context=_firing_context())
    by_id = {i.rule_id: i.rule_version for i in insights}
    assert by_id == {"br.canary.v1": 1, "br.canary.v2": 2}
