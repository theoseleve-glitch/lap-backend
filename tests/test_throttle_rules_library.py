"""T111+ (sprint2-backend) — throttle pickup rules library.

Authors `coaching/library/rules/throttle/` subtree; T111 lands the
first rule (`th.early_throttle.snap_oversteer`). Block H grows
this directory across T111-T120 (10 throttle-pickup rules).

Pinned behaviors:
  - directory loads cleanly (every YAML well-formed, RuleSpec-valid)
  - at least one throttle rule exists (T111 acceptance)
  - every rule's id starts with `th.` (namespacing — prevents a
    future copy-paste from `br.*` from silently shadowing via the
    loader's id-uniqueness check)
  - every rule is in the `technique` category — throttle pickup
    is single-corner skill, not multi-session strategy
  - the snap_oversteer rule fires when both conditions hold:
    throttle_pct_at_apex > 0.40 AND peak_rear_slip_angle_deg > 6.0
  - early-but-controlled (high throttle, low slip) stays silent
    — the rule keys on COMBINED early-and-loose, not "any early
    throttle"
  - controlled-loose (late throttle, high slip from another cause)
    stays silent — same conjunction protection from the other side
  - both signals at quiet values stay silent

Mutation: relaxing the threshold on either signal would silently
flag good drivers; dropping the conjunction would mis-attribute
slip from balance/setup issues to throttle technique.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from coaching.library.rule_loader import load_rules
from coaching.library.rule_runner import run_rules

REPO_ROOT = Path(__file__).resolve().parents[1]
THROTTLE_DIR = REPO_ROOT / "coaching" / "library" / "rules" / "throttle"

_SNAP_RULE_ID = "th.early_throttle.snap_oversteer"
_LATE_RULE_ID = "th.late_throttle.exit_speed_loss"


# Template vars referenced by the rule's explanations. Any
# firing-path test must carry these or the runner raises
# InsightRenderError. Listed once so mutation/maintenance is
# obvious — change the template, change this dict.
_TEMPLATE_VARS: dict[str, Any] = {
    "corner": "Turn 4",
    "throttle_pct_at_apex": 0.55,
    "peak_rear_slip_angle_deg": 8.5,
    # T112 — late-throttle signals.
    "exit_speed": 95.0,
    "reference_exit_speed": 100.0,
    "exit_speed_delta_kmh": 5,
}


def test_throttle_rules_directory_loads_cleanly() -> None:
    """Every throttle YAML must validate against RuleSpec. Pin: a
    malformed YAML or schema violation would crash the engine at
    startup; the loader must never raise on this path."""
    rules = load_rules(THROTTLE_DIR)
    assert rules, "expected at least one throttle rule"


def test_at_least_one_throttle_rule_authored() -> None:
    """T111 acceptance: at least one rule. Block H sets the
    runway for 10 throttle rules (T111-T120)."""
    rules = load_rules(THROTTLE_DIR)
    assert len(rules) >= 1, (
        f"expected at least 1 throttle rule, found "
        f"{len(rules)}: {[r.id for r in rules]}"
    )


def test_every_throttle_rule_id_starts_with_th_prefix() -> None:
    """Prefix collision check. A future contributor copying a
    `br.*` template into the throttle dir without renaming the
    id would silently shadow a braking rule via the loader's
    id-uniqueness check, and the rules engine would misroute
    evidence. Pin the namespacing convention."""
    rules = load_rules(THROTTLE_DIR)
    bad = [r.id for r in rules if not r.id.startswith("th.")]
    assert not bad, f"non-throttle ids in throttle dir: {bad}"


def test_every_throttle_rule_is_in_technique_category() -> None:
    """Throttle pickup is single-corner skill execution, not
    multi-session strategy. Calling these `strategy` would
    mis-rank them in the prescription queue (which separates
    strategic from tactical drills)."""
    rules = load_rules(THROTTLE_DIR)
    bad = [
        (r.id, r.category) for r in rules if r.category != "technique"
    ]
    assert not bad, (
        f"throttle rules with non-technique category: {bad}"
    )


def test_snap_oversteer_fires_on_combined_early_and_loose() -> None:
    """Both signals firing: throttle at apex = 0.55 (>0.40
    threshold), peak rear slip = 8.5° (>6.0 threshold). Pin:
    the canonical "snapped the rear by jumping on it too early"
    pattern produces an insight."""
    rules = load_rules(THROTTLE_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "throttle_pct_at_apex": 0.55,
            "peak_rear_slip_angle_deg": 8.5,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _SNAP_RULE_ID in fired_ids, (
        f"expected {_SNAP_RULE_ID} to fire on combined "
        f"early-and-loose context, got {sorted(fired_ids)}"
    )


def test_snap_oversteer_silent_when_throttle_late() -> None:
    """Throttle at apex = 0.20 (well below 0.40 — driver waited
    for the apex to commit). Even with high rear slip (could be
    setup issue), the rule must NOT misattribute it to throttle.
    Pin: the conjunction protects against false positives on
    drivers who aren't being early."""
    rules = load_rules(THROTTLE_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "throttle_pct_at_apex": 0.20,
            "peak_rear_slip_angle_deg": 8.5,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _SNAP_RULE_ID not in fired_ids, (
        f"{_SNAP_RULE_ID} fired on late-throttle / high-slip "
        f"context — mis-attributed setup slip to throttle. "
        f"Fired: {sorted(fired_ids)}"
    )


def test_snap_oversteer_silent_when_rear_under_control() -> None:
    """Throttle at apex = 0.55 (early), but peak rear slip = 3.0°
    (well-managed exit, no snap). The rule must NOT fire — early
    throttle that the chassis handles is fine technique. Pin:
    the conjunction prevents flagging brave-but-controlled
    drivers."""
    rules = load_rules(THROTTLE_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "throttle_pct_at_apex": 0.55,
            "peak_rear_slip_angle_deg": 3.0,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _SNAP_RULE_ID not in fired_ids, (
        f"{_SNAP_RULE_ID} fired on early-but-controlled "
        f"context — flagged a brave-but-clean driver. "
        f"Fired: {sorted(fired_ids)}"
    )


def test_snap_oversteer_silent_when_both_signals_quiet() -> None:
    """Late throttle + low slip — clean corner exit. Baseline
    no-fire to prove the rule isn't fires-always."""
    rules = load_rules(THROTTLE_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "throttle_pct_at_apex": 0.20,
            "peak_rear_slip_angle_deg": 3.0,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _SNAP_RULE_ID not in fired_ids


# -- T112: th.late_throttle.exit_speed_loss --------------------


def test_late_throttle_fires_on_combined_late_and_slow_exit() -> None:
    """Both signals firing: throttle at apex = 0.10 (driver
    coasted past the apex without committing), exit speed = 92
    vs reference 100 (8% deficit, well below the 95% threshold).
    Pin: the canonical "waited too long, lost the run" pattern
    produces an insight."""
    rules = load_rules(THROTTLE_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "throttle_pct_at_apex": 0.10,
            "peak_rear_slip_angle_deg": 3.0,
            "exit_speed": 92.0,
            "reference_exit_speed": 100.0,
            "exit_speed_delta_kmh": 8,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _LATE_RULE_ID in fired_ids, (
        f"expected {_LATE_RULE_ID} to fire on late-throttle / "
        f"slow-exit context, got {sorted(fired_ids)}"
    )


def test_late_throttle_silent_when_throttle_committed_early() -> None:
    """Throttle at apex = 0.50 (committed early), exit speed
    still down at 92 (8% deficit — could be a setup or grip
    issue). The rule must NOT fire — committing early but
    losing exit is a different problem (grip, setup, line)."""
    rules = load_rules(THROTTLE_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "throttle_pct_at_apex": 0.50,
            "peak_rear_slip_angle_deg": 3.0,
            "exit_speed": 92.0,
            "reference_exit_speed": 100.0,
            "exit_speed_delta_kmh": 8,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _LATE_RULE_ID not in fired_ids, (
        f"{_LATE_RULE_ID} fired on early-throttle / slow-exit — "
        f"mis-attributed grip or setup issue to throttle timing. "
        f"Fired: {sorted(fired_ids)}"
    )


def test_late_throttle_silent_when_exit_speed_on_pace() -> None:
    """Throttle at apex = 0.10 (late), but exit speed = 99 vs
    reference 100 (within 1% — driver "wasted" some opportunity
    but didn't actually lose meaningful time). The rule must NOT
    fire — coaching every quiet apex would flood drivers with
    non-actionable noise on flowing-line corners."""
    rules = load_rules(THROTTLE_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "throttle_pct_at_apex": 0.10,
            "peak_rear_slip_angle_deg": 3.0,
            "exit_speed": 99.0,
            "reference_exit_speed": 100.0,
            "exit_speed_delta_kmh": 1,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _LATE_RULE_ID not in fired_ids, (
        f"{_LATE_RULE_ID} fired on late-throttle / on-pace exit — "
        f"floor noise. Fired: {sorted(fired_ids)}"
    )
