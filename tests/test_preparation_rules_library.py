"""T091 (sprint2-backend) — target preparation milestone rule.

Authors a `coaching/library/rules/preparation/` subtree carrying
the milestone rule that fires when the driver's pinned target
event is 3 or fewer sessions away. The rule pulls the driver
into the prescription queue with a target-specific drill so
preparation actually happens before the event, not after.

Pinned behaviors:
  - directory loads cleanly (every YAML well-formed, RuleSpec-valid)
  - at least one preparation rule exists (T091 acceptance)
  - every rule's id starts with `prep.` (namespacing — prevents
    a future copy-paste from `enduro.*` from silently shadowing
    via the loader's id-uniqueness check)
  - every rule is in the `strategy` category — preparation is
    multi-session planning, not single-corner technique
  - the milestone rule fires at exactly 3 sessions remaining
    (boundary: <= 3 must include the boundary value)
  - the milestone rule stays silent at 4 sessions remaining
    (boundary: 4 must NOT fire)
  - the milestone rule stays silent at 999 sessions remaining
    (sentinel: sprint-only drivers without a pinned event are
    coded with high sentinel values; coaching them about a
    "target event" they never set would be a UX bug)

Mutation: flipping `<=` to `<` breaks the boundary-3 firing test;
removing the rule file breaks the count + firing tests; renaming
the id to drop the `prep.` prefix breaks the namespacing test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from coaching.library.rule_loader import load_rules
from coaching.library.rule_runner import run_rules

REPO_ROOT = Path(__file__).resolve().parents[1]
PREP_DIR = REPO_ROOT / "coaching" / "library" / "rules" / "preparation"

_MILESTONE_RULE_ID = "prep.target.milestone_3_sessions_remaining"


# Template vars referenced by the rule's explanation. Any rule
# that fires must carry these or the runner raises
# InsightRenderError. Listed here once so mutation/maintenance is
# obvious — change the template, change this dict.
_TEMPLATE_VARS: dict[str, Any] = {
    "target_event_name": "Spa 6h Endurance",
    "sessions_remaining": 3,
}


def test_preparation_rules_directory_loads_cleanly() -> None:
    """Every preparation YAML must validate against RuleSpec.
    Pin: a malformed YAML or a schema violation would crash the
    engine at startup; the loader must never raise on this path."""
    rules = load_rules(PREP_DIR)
    assert rules, "expected at least one preparation rule"


def test_at_least_one_preparation_rule_authored() -> None:
    """T091 acceptance: at least one rule. Without the rule, the
    target-aware prescription queue has nothing to enqueue when
    the event window opens."""
    rules = load_rules(PREP_DIR)
    assert len(rules) >= 1, (
        f"expected at least 1 preparation rule, found {len(rules)}: "
        f"{[r.id for r in rules]}"
    )


def test_every_preparation_rule_id_starts_with_prep_prefix() -> None:
    """Prefix collision check. A future contributor copying an
    `enduro.*` template into the preparation dir without renaming
    the id would silently shadow a strategy rule via the loader's
    id-uniqueness check, and the rules engine would misroute
    evidence. Pin the namespacing convention."""
    rules = load_rules(PREP_DIR)
    bad = [r.id for r in rules if not r.id.startswith("prep.")]
    assert not bad, f"non-preparation ids in preparation dir: {bad}"


def test_every_preparation_rule_is_in_strategy_category() -> None:
    """Preparation is multi-session planning (event-aware drill
    selection, milestone reminders), not single-corner technique.
    Calling these `technique` would conflate them with braking/
    cornering rules and mis-rank them in the prescription queue."""
    rules = load_rules(PREP_DIR)
    bad = [(r.id, r.category) for r in rules if r.category != "strategy"]
    assert not bad, f"preparation rules with non-strategy category: {bad}"


def test_milestone_rule_fires_at_3_sessions_remaining() -> None:
    """Boundary-inclusive firing: when sessions_until_target_event
    equals exactly 3, the milestone rule must fire. The whole
    point of a "3 sessions left" milestone is to prompt at the
    threshold; <= must include 3 itself."""
    rules = load_rules(PREP_DIR)
    insights = run_rules(
        rules,
        context={**_TEMPLATE_VARS, "sessions_until_target_event": 3},
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _MILESTONE_RULE_ID in fired_ids, (
        f"expected {_MILESTONE_RULE_ID} to fire at 3 sessions, "
        f"got {sorted(fired_ids)}"
    )


def test_milestone_rule_silent_at_4_sessions_remaining() -> None:
    """Boundary-exclusive non-firing: at 4 sessions out, the
    milestone has not yet been reached. Pin: a regression that
    flipped `<=` to `<` would leave 3-sessions firing but also
    leave nothing firing — wait, that'd still pass the firing
    test if `<` and value=3 → False. The boundary-pair pins both
    sides. A regression to `< 4` would also pass both. Pinning
    against an inadvertent broader trigger (e.g., `<= 5`) is the
    real value here."""
    rules = load_rules(PREP_DIR)
    insights = run_rules(
        rules,
        context={**_TEMPLATE_VARS, "sessions_until_target_event": 4},
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _MILESTONE_RULE_ID not in fired_ids, (
        f"{_MILESTONE_RULE_ID} unexpectedly fired at 4 sessions; "
        f"all fired: {sorted(fired_ids)}"
    )


def test_milestone_rule_silent_for_sprint_only_driver() -> None:
    """Sprint-only drivers (no TargetEvent pinned) must not be
    coached about a target event they never created. The runner
    convention is that callers always include the signal, set to
    a high sentinel (999) when no pin exists — production callers
    derive this from the readiness recompute hook (T089). Pin: a
    rule condition typo that omitted the upper-bound check (e.g.,
    `True` or always-fires) would coach every sprint driver about
    "their event" — a glaring UX bug."""
    rules = load_rules(PREP_DIR)
    insights = run_rules(
        rules,
        context={**_TEMPLATE_VARS, "sessions_until_target_event": 999},
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _MILESTONE_RULE_ID not in fired_ids, (
        f"{_MILESTONE_RULE_ID} unexpectedly fired for sprint-only "
        f"sentinel (999 sessions); all fired: {sorted(fired_ids)}"
    )
