"""T185 — coaching rule runner.

Integrates the four primitives:
  - schema    (T181) for the rule shape
  - loader    (T182) for `load_rules`
  - compiler  (T183) for predicates
  - namespace (T184) for known feature names

Given a feature context dict, evaluates every rule's predicate
and emits a `CoachingInsight` for each that fires. Templates use
mustache-style `{{var}}` substitution with values from the
context.
"""

from pathlib import Path

import pytest

from coaching.library.rule_loader import load_rules
from coaching.library.rule_runner import (
    CoachingInsight,
    InsightRenderError,
    run_rules,
)
from coaching.library.rule_schema import RuleSpec

ADR_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "coaching"
    / "adr_013_example.yaml"
)


def _load_adr_rule() -> RuleSpec:
    """Read the canonical fixture as a single RuleSpec."""
    import yaml

    return RuleSpec.model_validate(
        yaml.safe_load(ADR_FIXTURE.read_text(encoding="utf-8"))
    )


def test_empty_rules_returns_empty_insights() -> None:
    insights = run_rules([], context={})
    assert insights == []


def test_rule_that_fires_produces_insight() -> None:
    rule = _load_adr_rule()
    # apex_speed (92) < reference_apex_speed (100) * 0.93 = 93 → True
    context = {
        "apex_speed": 92.0,
        "reference_apex_speed": 100.0,
        "delta_kmh": 8.0,
        "corner": "Turn 3",
    }
    insights = run_rules([rule], context=context)
    assert len(insights) == 1
    assert insights[0].rule_id == rule.id


def test_rule_that_does_not_fire_produces_no_insight() -> None:
    rule = _load_adr_rule()
    # 96 < 93 is False → no insight.
    context = {
        "apex_speed": 96.0,
        "reference_apex_speed": 100.0,
        "delta_kmh": 4.0,
        "corner": "Turn 3",
    }
    insights = run_rules([rule], context=context)
    assert insights == []


def test_insight_carries_category_and_subcategory() -> None:
    rule = _load_adr_rule()
    context = {
        "apex_speed": 80.0,
        "reference_apex_speed": 100.0,
        "delta_kmh": 20.0,
        "corner": "Turn 1",
    }
    [insight] = run_rules([rule], context=context)
    assert insight.category == "technique"
    assert insight.subcategory == "late_braking"


def test_explanation_renders_template_variables() -> None:
    """ADR-013's templates use `{{delta_kmh}}` and `{{corner}}`.
    The runner must substitute from the context."""
    rule = _load_adr_rule()
    context = {
        "apex_speed": 85.0,
        "reference_apex_speed": 100.0,
        "delta_kmh": 15.0,
        "corner": "Turn 7",
    }
    [insight] = run_rules([rule], context=context, difficulty="beginner")
    assert "15" in insight.explanation
    assert "Turn 7" in insight.explanation
    # The placeholders themselves should be gone.
    assert "{{delta_kmh}}" not in insight.explanation
    assert "{{corner}}" not in insight.explanation


def test_difficulty_selects_template_branch() -> None:
    rule = _load_adr_rule()
    context = {
        "apex_speed": 85.0,
        "reference_apex_speed": 100.0,
        "delta_kmh": 15.0,
        "corner": "Turn 7",
        "release_ms": 250,
    }
    beginner = run_rules([rule], context=context, difficulty="beginner")
    advanced = run_rules([rule], context=context, difficulty="advanced")
    # The branches in ADR-013 are distinguishable by phrasing.
    assert "braking too late" in beginner[0].explanation.lower()
    assert "derivative" in advanced[0].explanation.lower()


def test_missing_template_variable_raises() -> None:
    """If a rule's template references `{{delta_kmh}}` but the
    context doesn't carry it, fail loudly — silent insertion of
    blanks is worse than no insight at all."""
    rule = _load_adr_rule()
    context = {
        "apex_speed": 85.0,
        "reference_apex_speed": 100.0,
        # Intentionally missing delta_kmh and corner.
    }
    with pytest.raises(InsightRenderError) as exc_info:
        run_rules([rule], context=context)
    assert "delta_kmh" in str(exc_info.value)


def test_runs_multiple_rules_independently(tmp_path: Path) -> None:
    """Loader → runner end-to-end on a directory with two rules.
    Only the firing one shows up in insights."""
    src = ADR_FIXTURE.read_text(encoding="utf-8")
    (tmp_path / "fires.yaml").write_text(
        src.replace(
            'id: "br.late_braking.apex_speed_loss"',
            'id: "br.fires.x"',
        ),
        encoding="utf-8",
    )
    # Rule that won't fire — flip the comparison.
    inert = src.replace(
        'id: "br.late_braking.apex_speed_loss"',
        'id: "br.inert.y"',
    ).replace(
        "apex_speed < reference_apex_speed * 0.93",
        "apex_speed > reference_apex_speed * 1.5",
    )
    (tmp_path / "inert.yaml").write_text(inert, encoding="utf-8")

    rules = load_rules(tmp_path)
    context = {
        "apex_speed": 85.0,
        "reference_apex_speed": 100.0,
        "delta_kmh": 15.0,
        "corner": "Turn 1",
    }
    insights = run_rules(rules, context=context)
    fired_ids = {i.rule_id for i in insights}
    assert fired_ids == {"br.fires.x"}


def test_insight_carries_difficulty_label() -> None:
    """Downstream consumers (UI, analytics) need to know which
    template branch was rendered — bake it onto the insight."""
    rule = _load_adr_rule()
    context = {
        "apex_speed": 85.0,
        "reference_apex_speed": 100.0,
        "delta_kmh": 15.0,
        "corner": "T1",
        "release_ms": 250,
    }
    [insight] = run_rules(
        [rule], context=context, difficulty="intermediate"
    )
    assert insight.difficulty == "intermediate"
    assert isinstance(insight, CoachingInsight)
