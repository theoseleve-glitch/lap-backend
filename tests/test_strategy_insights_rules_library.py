"""T104 (sprint2-backend) — strategy execution insights rule.

Authors a `coaching/library/rules/strategy/` subtree carrying the
"low execution" rule that fires when T102's StrategyPlan.execution
_score is under 70/100 — the threshold beyond which the
plan-vs-actual divergence is meaningful enough to coach on (the
score is bounded [0, 100] with weights 0.50/0.25/0.25, so 70
corresponds roughly to a single multi-lap pit miss or a
substantial fuel/stint delta).

Pinned behaviors:
  - directory loads cleanly (every YAML well-formed, RuleSpec-valid)
  - at least one strategy-insights rule exists (T104 acceptance)
  - every rule's id starts with `strat.` (namespacing — prevents
    a future copy-paste from `enduro.*` or `prep.*` from silently
    shadowing via the loader's id-uniqueness check)
  - every rule is in the `strategy` category — execution-vs-plan
    is multi-lap planning, not single-corner technique
  - the low-execution rule fires at score=69 (boundary: < 70 must
    include 69 itself)
  - the low-execution rule stays silent at score=70 (boundary
    exclusive — 70 is the threshold, not "above")
  - the low-execution rule stays silent at score=85 (clearly
    "good enough"; pins against an inadvertent inversion or a
    drift to e.g. `< 90`)
  - the rendered explanation surfaces the pit_lap_delta value so
    the driver sees the WHY, not just the score — the whole point
    of T104 is "missed pit window by 3 laps"-style guidance, not
    a bare "plan execution was poor" message

Mutation: flipping `<` to `<=` would still fire at 69 but leak at
70, breaking the silence test; dropping `{{pit_lap_delta_total}}`
from the template breaks the explanation-content test; renaming
the id to drop `strat.` breaks the namespacing test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from coaching.library.rule_loader import load_rules
from coaching.library.rule_runner import run_rules

REPO_ROOT = Path(__file__).resolve().parents[1]
STRAT_DIR = REPO_ROOT / "coaching" / "library" / "rules" / "strategy"

_LOW_EXEC_RULE_ID = "strat.execution.low_score"


# Template vars referenced by the rule's explanations. Every
# firing-path test must carry these or the runner raises
# InsightRenderError. Listed once so mutation/maintenance is
# obvious — change the template, change this dict.
_TEMPLATE_VARS: dict[str, Any] = {
    "pit_lap_delta_total": 3,
    "fuel_delta_liters": 4.5,
    "stint_duration_delta_laps": 2,
}


def test_strategy_insights_directory_loads_cleanly() -> None:
    """Every strategy YAML must validate against RuleSpec. Pin: a
    malformed YAML or schema violation would crash the engine at
    startup; the loader must never raise on this path."""
    rules = load_rules(STRAT_DIR)
    assert rules, "expected at least one strategy-insights rule"


def test_at_least_one_strategy_insights_rule_authored() -> None:
    """T104 acceptance: at least one rule. Without it, the
    plan-vs-actual divergence pipeline produces a score but no
    coaching insight — the driver never sees the actionable
    "missed pit window" guidance the score is supposed to surface."""
    rules = load_rules(STRAT_DIR)
    assert len(rules) >= 1, (
        f"expected at least 1 strategy-insights rule, found "
        f"{len(rules)}: {[r.id for r in rules]}"
    )


def test_every_strategy_insights_rule_id_starts_with_strat_prefix() -> None:
    """Prefix collision check. A future contributor copying an
    `enduro.*` or `prep.*` template into the strategy dir without
    renaming the id would silently shadow another rule via the
    loader's id-uniqueness check, and the rules engine would
    misroute evidence. Pin the namespacing convention."""
    rules = load_rules(STRAT_DIR)
    bad = [r.id for r in rules if not r.id.startswith("strat.")]
    assert not bad, f"non-strategy ids in strategy dir: {bad}"


def test_every_strategy_insights_rule_is_in_strategy_category() -> None:
    """Plan-vs-actual execution is multi-lap planning, not
    single-corner technique. Calling these `technique` would
    conflate them with braking/cornering rules and mis-rank them
    in the prescription queue (which separates strategic from
    tactical drills)."""
    rules = load_rules(STRAT_DIR)
    bad = [
        (r.id, r.category) for r in rules if r.category != "strategy"
    ]
    assert not bad, (
        f"strategy-insights rules with non-strategy category: {bad}"
    )


def test_low_execution_rule_fires_at_score_69() -> None:
    """Boundary-inclusive firing: when strategy_execution_score
    equals exactly 69, the low-execution rule must fire. The whole
    point of a "<70" threshold is to prompt at the threshold; the
    boundary itself is included."""
    rules = load_rules(STRAT_DIR)
    insights = run_rules(
        rules,
        context={**_TEMPLATE_VARS, "strategy_execution_score": 69},
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _LOW_EXEC_RULE_ID in fired_ids, (
        f"expected {_LOW_EXEC_RULE_ID} to fire at score=69, "
        f"got {sorted(fired_ids)}"
    )


def test_low_execution_rule_silent_at_score_70() -> None:
    """Boundary-exclusive non-firing: at score=70 the rule must
    stay silent. Pin: regression to `<=` would silently start
    coaching every "exactly threshold" session, inflating the
    insight stream with non-actionable noise. The exclusive-vs-
    inclusive boundary pair pins both sides."""
    rules = load_rules(STRAT_DIR)
    insights = run_rules(
        rules,
        context={**_TEMPLATE_VARS, "strategy_execution_score": 70},
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _LOW_EXEC_RULE_ID not in fired_ids, (
        f"{_LOW_EXEC_RULE_ID} unexpectedly fired at score=70; "
        f"all fired: {sorted(fired_ids)}"
    )


def test_low_execution_rule_silent_at_score_85() -> None:
    """A "good enough" execution must not coach. Pin: a regression
    inverting the comparator (e.g., `>` instead of `<`) or
    drifting the threshold high (e.g., `< 90`) would coach every
    session — a glaring UX bug for drivers actually executing
    well."""
    rules = load_rules(STRAT_DIR)
    insights = run_rules(
        rules,
        context={**_TEMPLATE_VARS, "strategy_execution_score": 85},
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _LOW_EXEC_RULE_ID not in fired_ids, (
        f"{_LOW_EXEC_RULE_ID} unexpectedly fired at score=85; "
        f"all fired: {sorted(fired_ids)}"
    )


def test_low_execution_rule_explanation_surfaces_pit_lap_delta() -> None:
    """Pin the user-visible payload: the explanation must include
    the actual pit-lap delta number, not just say "execution was
    poor". The whole reason T104 exists is to give drivers
    actionable content like "missed pit window by 3 laps" — a
    rule that fires but renders a generic message would satisfy
    the firing test while failing the spec.

    Mutation: dropping `{{pit_lap_delta_total}}` from the template
    leaves the explanation passing schema but losing its actionable
    content; this test catches that."""
    rules = load_rules(STRAT_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "pit_lap_delta_total": 3,
            "strategy_execution_score": 45,
        },
    )
    fired = [
        ins for ins in insights if ins.rule_id == _LOW_EXEC_RULE_ID
    ]
    assert fired, "rule did not fire — cannot inspect explanation"
    explanation = fired[0].explanation
    assert "3" in explanation, (
        f"expected pit_lap_delta_total=3 to appear in explanation, "
        f"got: {explanation!r}"
    )
    assert "{{" not in explanation, (
        f"explanation has unrendered placeholder: {explanation!r}"
    )
