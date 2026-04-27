"""T121+ (sprint2-backend) — cornering / minimum-speed rules library.

Authors `coaching/library/rules/cornering/` subtree; T121 lands the
first rule (`cs.min_speed.too_low_late_apex`). Block H grows
this directory across T121-T130 (10 cornering rules).

Pinned behaviors:
  - directory loads cleanly (every YAML well-formed, RuleSpec-valid)
  - at least one cornering rule exists (T121 acceptance)
  - every rule's id starts with `cs.` (namespacing — prevents a
    future copy-paste from `th.*` or `br.*` from silently shadowing
    via the loader's id-uniqueness check)
  - every rule is in the `technique` category — cornering line/
    apex/min-speed is single-corner skill, not multi-session strategy
  - the late-apex rule fires when both conditions hold:
    min_corner_speed_deficit_pct >= 0.05 AND apex_position_pct_through_corner >= 0.60
  - speed-on-pace + late apex stays silent — late apex that didn't
    cost min speed isn't actionable (could be a deliberate
    late-apex line on a long-exit corner)
  - speed-deficit + on-time apex stays silent — speed deficit from
    another cause (entry, setup, grip) must NOT be mis-attributed
    to apex placement

Mutation: relaxing either threshold would flag drivers with a
deliberate late-apex line that pays off in exit speed; dropping
the conjunction would mis-attribute setup/grip-driven speed
deficits to apex technique.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from coaching.library.rule_loader import load_rules
from coaching.library.rule_runner import run_rules

REPO_ROOT = Path(__file__).resolve().parents[1]
CORNERING_DIR = REPO_ROOT / "coaching" / "library" / "rules" / "cornering"

_LATE_APEX_RULE_ID = "cs.min_speed.too_low_late_apex"
_WIDE_LINE_RULE_ID = "cs.min_speed.too_high_wide_line"
_EARLY_TURN_RULE_ID = "cs.apex_miss.early_turn"
_LATE_TURN_RULE_ID = "cs.apex_miss.late_turn"


# Template vars referenced by the rule's explanations. Any
# firing-path test must carry these or the runner raises
# InsightRenderError.
_TEMPLATE_VARS: dict[str, Any] = {
    "corner": "Turn 4",
    "min_corner_speed_deficit_pct": 0.08,
    "apex_position_pct_through_corner": 0.70,
    # T122 — too-high min speed / wide-line signals.
    "min_corner_speed_excess_pct": 0.07,
    "apex_lateral_offset_m": 0.80,
    # T123 — early-turn / early-apex signal (apex_position
    # already declared above for T121).
    "turn_in_distance_m_before_optimal": 8.0,
    # T124 — late-turn / late-apex signal (apex_position
    # at 0.70 above already firing-side for late apex).
    "turn_in_distance_m_after_optimal": 8.0,
}


def test_cornering_rules_directory_loads_cleanly() -> None:
    """Every cornering YAML must validate against RuleSpec. Pin: a
    malformed YAML or schema violation would crash the engine at
    startup; the loader must never raise on this path."""
    rules = load_rules(CORNERING_DIR)
    assert rules, "expected at least one cornering rule"


def test_at_least_one_cornering_rule_authored() -> None:
    """T121 acceptance: at least one rule. Block H sets the
    runway for 10 cornering rules (T121-T130)."""
    rules = load_rules(CORNERING_DIR)
    assert len(rules) >= 1, (
        f"expected at least 1 cornering rule, found "
        f"{len(rules)}: {[r.id for r in rules]}"
    )


def test_every_cornering_rule_id_starts_with_cs_prefix() -> None:
    """Prefix collision check. A future contributor copying a
    `th.*` or `br.*` template into the cornering dir without
    renaming the id would silently shadow another namespace via
    the loader's id-uniqueness check, and the rules engine would
    misroute evidence. Pin the namespacing convention."""
    rules = load_rules(CORNERING_DIR)
    bad = [r.id for r in rules if not r.id.startswith("cs.")]
    assert not bad, f"non-cornering ids in cornering dir: {bad}"


def test_every_cornering_rule_is_in_technique_category() -> None:
    """Cornering line/apex is single-corner skill execution, not
    multi-session strategy. Calling these `strategy` would mis-
    rank them in the prescription queue (which separates strategic
    from tactical drills)."""
    rules = load_rules(CORNERING_DIR)
    bad = [
        (r.id, r.category) for r in rules if r.category != "technique"
    ]
    assert not bad, (
        f"cornering rules with non-technique category: {bad}"
    )


# -- T121: cs.min_speed.too_low_late_apex ----------------------


def test_late_apex_fires_on_combined_speed_deficit_and_late_apex() -> None:
    """Both signals firing: min-speed deficit 0.08 (8% below
    reference — clearly slow) AND apex at 70% through the corner
    (well past the 60% late-apex threshold). Pin: the canonical
    "rotated late so you killed the apex" pattern produces an
    insight rather than firing on every slow corner regardless
    of cause."""
    rules = load_rules(CORNERING_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "min_corner_speed_deficit_pct": 0.08,
            "apex_position_pct_through_corner": 0.70,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _LATE_APEX_RULE_ID in fired_ids, (
        f"expected {_LATE_APEX_RULE_ID} to fire on combined "
        f"speed-deficit + late-apex context, got "
        f"{sorted(fired_ids)}"
    )


def test_late_apex_silent_when_speed_on_pace() -> None:
    """Apex at 70% (late) but speed deficit only 0.01 (driver
    apexed late but it didn't cost min speed — could be a long-
    exit corner that rewards a deliberate late apex). The rule
    must NOT fire — late apex without a speed cost isn't a
    mistake. Pin: the conjunction prevents flagging deliberate
    late-apex lines that pay off in exit."""
    rules = load_rules(CORNERING_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "min_corner_speed_deficit_pct": 0.01,
            "apex_position_pct_through_corner": 0.70,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _LATE_APEX_RULE_ID not in fired_ids, (
        f"{_LATE_APEX_RULE_ID} fired on late-apex / on-pace "
        f"context — flagged a deliberate late-apex line. "
        f"Fired: {sorted(fired_ids)}"
    )


def test_late_apex_silent_when_apex_on_time() -> None:
    """Speed deficit 0.08 (driver IS slow) but apex at 50%
    (geometric middle — apex placement is correct). The rule
    must NOT fire — speed deficit must come from elsewhere
    (entry technique, setup, grip), and coaching "you apexed
    late" would be wrong. Pin: the conjunction prevents mis-
    attributing speed deficits to apex placement."""
    rules = load_rules(CORNERING_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "min_corner_speed_deficit_pct": 0.08,
            "apex_position_pct_through_corner": 0.50,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _LATE_APEX_RULE_ID not in fired_ids, (
        f"{_LATE_APEX_RULE_ID} fired on slow-but-on-time-apex "
        f"context — mis-attributed speed deficit to apex placement. "
        f"Fired: {sorted(fired_ids)}"
    )


# -- T122: cs.min_speed.too_high_wide_line ---------------------


def test_wide_line_fires_on_combined_speed_excess_and_offset() -> None:
    """Both signals firing: min-speed excess 0.07 (7% above
    reference — carrying too much) AND lateral offset of 0.80m
    from optimal apex (well past the 0.50m wide-of-apex floor).
    Pin: the canonical "carried too much speed and got pushed
    wide" pattern produces an insight rather than firing on
    every fast-through-the-corner driver."""
    rules = load_rules(CORNERING_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "min_corner_speed_excess_pct": 0.07,
            "apex_lateral_offset_m": 0.80,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _WIDE_LINE_RULE_ID in fired_ids, (
        f"expected {_WIDE_LINE_RULE_ID} to fire on combined "
        f"speed-excess + wide-offset context, got "
        f"{sorted(fired_ids)}"
    )


def test_wide_line_silent_when_speed_on_pace() -> None:
    """Lateral offset 0.80m (wide line) but speed excess only
    0.01 (driver is on a wide line but not carrying excess
    speed — could be a deliberate wide entry for traffic, an
    overtake setup, or a defensive line). The rule must NOT
    fire — wide line without excess speed isn't a mistake.
    Pin: the conjunction prevents flagging tactical wide
    lines as min-speed-too-high errors."""
    rules = load_rules(CORNERING_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "min_corner_speed_excess_pct": 0.01,
            "apex_lateral_offset_m": 0.80,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _WIDE_LINE_RULE_ID not in fired_ids, (
        f"{_WIDE_LINE_RULE_ID} fired on wide-line / on-pace "
        f"context — flagged a tactical wide line. "
        f"Fired: {sorted(fired_ids)}"
    )


def test_wide_line_silent_when_on_proper_line() -> None:
    """Speed excess 0.07 (driver IS faster) but apex offset
    only 0.10m (on the proper line). The rule must NOT fire —
    a faster driver on the right line is just better, not
    wrong. Pin: the conjunction prevents flagging better
    drivers as making min-speed errors."""
    rules = load_rules(CORNERING_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "min_corner_speed_excess_pct": 0.07,
            "apex_lateral_offset_m": 0.10,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _WIDE_LINE_RULE_ID not in fired_ids, (
        f"{_WIDE_LINE_RULE_ID} fired on faster-but-on-line "
        f"context — flagged a faster driver as wrong. "
        f"Fired: {sorted(fired_ids)}"
    )


# -- T123: cs.apex_miss.early_turn -----------------------------


def test_early_turn_fires_on_combined_early_turn_and_early_apex() -> None:
    """Both signals firing: turn-in 8m before the optimal
    point AND apex hit at 30% through the corner (well before
    the 40% early-apex floor). Pin: the canonical "turned in
    too early, hit the apex too soon, ran out wide on exit"
    pattern produces an insight rather than firing on every
    early-apex line (which can be a deliberate kerb cut)."""
    rules = load_rules(CORNERING_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "turn_in_distance_m_before_optimal": 8.0,
            "apex_position_pct_through_corner": 0.30,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _EARLY_TURN_RULE_ID in fired_ids, (
        f"expected {_EARLY_TURN_RULE_ID} to fire on combined "
        f"early-turn + early-apex context, got "
        f"{sorted(fired_ids)}"
    )


def test_early_turn_silent_when_turn_in_on_time() -> None:
    """Apex at 30% (early) but turn-in 1m before optimal
    (essentially on time — could be a deliberate cut over an
    inside kerb that drops the apex point earlier). The rule
    must NOT fire — early-apex from a tactical kerb cut is
    not a turn-in error. Pin: the conjunction prevents flagging
    deliberate kerb-cutting technique."""
    rules = load_rules(CORNERING_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "turn_in_distance_m_before_optimal": 1.0,
            "apex_position_pct_through_corner": 0.30,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _EARLY_TURN_RULE_ID not in fired_ids, (
        f"{_EARLY_TURN_RULE_ID} fired on on-time-turn / early-"
        f"apex context — flagged a deliberate kerb cut. "
        f"Fired: {sorted(fired_ids)}"
    )


def test_early_turn_silent_when_apex_on_time() -> None:
    """Turn-in 8m before optimal (early) but apex at 50% (on
    time — driver turned in early but recovered with smooth
    rotation / late brake release, landing the apex correctly).
    The rule must NOT fire — turn-in timing alone doesn't
    matter if the apex still lands right. Pin: the conjunction
    prevents flagging recoverable early-turn-in mistakes that
    didn't propagate to apex placement."""
    rules = load_rules(CORNERING_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "turn_in_distance_m_before_optimal": 8.0,
            "apex_position_pct_through_corner": 0.50,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _EARLY_TURN_RULE_ID not in fired_ids, (
        f"{_EARLY_TURN_RULE_ID} fired on early-turn / on-time-"
        f"apex context — flagged a recovered turn-in. "
        f"Fired: {sorted(fired_ids)}"
    )


# -- T124: cs.apex_miss.late_turn ------------------------------


def test_late_turn_fires_on_combined_late_turn_and_late_apex() -> None:
    """Both signals firing: turn-in 8m past the optimal point
    AND apex at 70% through the corner (well past the 60% late-
    apex floor). Pin: the canonical "turned in too late, hit
    the apex too late, compressed the rotation, killed exit"
    pattern produces an insight; coexists cleanly with T121
    (which keys off speed-deficit instead of turn-in timing)."""
    rules = load_rules(CORNERING_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "turn_in_distance_m_after_optimal": 8.0,
            "apex_position_pct_through_corner": 0.70,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _LATE_TURN_RULE_ID in fired_ids, (
        f"expected {_LATE_TURN_RULE_ID} to fire on combined "
        f"late-turn + late-apex context, got "
        f"{sorted(fired_ids)}"
    )


def test_late_turn_silent_when_turn_in_on_time() -> None:
    """Apex at 70% (late) but turn-in 1m past optimal (on
    time). The rule must NOT fire — late apex from another
    cause (lazy rotation, see T121's territory) must not be
    mis-attributed to turn-in timing. Pin: the conjunction
    keeps T121 and T124 cleanly separated by cause."""
    rules = load_rules(CORNERING_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "turn_in_distance_m_after_optimal": 1.0,
            "apex_position_pct_through_corner": 0.70,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _LATE_TURN_RULE_ID not in fired_ids, (
        f"{_LATE_TURN_RULE_ID} fired on on-time-turn / late-"
        f"apex context — mis-attributed rotation cause to "
        f"turn-in timing. Fired: {sorted(fired_ids)}"
    )


def test_late_turn_silent_when_apex_on_time() -> None:
    """Turn-in 8m past optimal (late) but apex at 50% (on time
    — driver turned in late but recovered with sharp rotation,
    landing the apex correctly). The rule must NOT fire — late
    turn-in alone isn't a problem if the apex still lands right.
    Pin: the conjunction prevents flagging recovered late-turn
    mistakes that didn't propagate to apex placement."""
    rules = load_rules(CORNERING_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "turn_in_distance_m_after_optimal": 8.0,
            "apex_position_pct_through_corner": 0.50,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _LATE_TURN_RULE_ID not in fired_ids, (
        f"{_LATE_TURN_RULE_ID} fired on late-turn / on-time-"
        f"apex context — flagged a recovered turn-in. "
        f"Fired: {sorted(fired_ids)}"
    )
