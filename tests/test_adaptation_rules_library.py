"""T151+ (sprint2-backend) — adaptation + conditions rules library.

Authors `coaching/library/rules/adaptation/` subtree; T151 lands the
first rule (`ad.grip_change.slow_to_adapt`). Block I grows this
directory across T151-T160 (10 adaptation + conditions rules).

Pinned behaviors:
  - directory loads cleanly (every YAML well-formed, RuleSpec-valid)
  - at least one adaptation rule exists (T151 acceptance)
  - every rule's id starts with `ad.` — adaptation is its own
    namespace (track / weather / setup / car / pressure response).
    The loader's id-uniqueness check catches cross-namespace
    collisions but the directory-level prefix guard catches a
    future copy-paste from `tm.*` / `fm.*` / `co.*` / etc.
  - every rule is in the `strategy` category — adaptation patterns
    play out across multiple laps and span resource decisions
    (when to commit harder, when to pull back), not single-corner
    skill execution.
  - the slow-grip-adapt rule fires when both conditions hold:
    grip_level_change_magnitude_pct >= 0.08 AND
    laps_to_input_recalibration >= 4
  - grip-change-without-slow-adapt keeps the rule silent — driver
    recalibrating in 1-2 laps is correct technique, not a
    coachable failure mode.
  - slow-recalibration-without-grip-change keeps the rule silent —
    "took 5 laps to find the brake markers" only matters if the
    grip actually moved; otherwise the prescription is wrong
    (driver was on the right markers all along).

Mutation: dropping the magnitude guard would coach "adapt to grip
changes" to drivers whose grip didn't actually change. Dropping the
recalibration guard would coach "adapt faster" to drivers who
already adapted promptly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from coaching.library.rule_loader import load_rules
from coaching.library.rule_runner import run_rules

REPO_ROOT = Path(__file__).resolve().parents[1]
ADAPTATION_DIR = REPO_ROOT / "coaching" / "library" / "rules" / "adaptation"

_GRIP_CHANGE_RULE_ID = "ad.grip_change.slow_to_adapt"


# Template vars referenced by the rule's explanations. Any
# firing-path test must carry these or the runner raises
# InsightRenderError.
_TEMPLATE_VARS: dict[str, Any] = {
    # T151 — grip-change adaptation. Caller supplies the magnitude
    # (abs value) of the grip-level change between consecutive lap
    # windows; condition compiler doesn't allow function calls so
    # the abs() is computed upstream by the feature builder. Same
    # magnitude framing as T134 / T142 — covers grip increases
    # (track rubbering in, drying after rain) and grip decreases
    # (rain starting, tire degradation) under one threshold. The
    # `laps_to_input_recalibration` signal is the count of laps
    # before the driver's brake-pressure / throttle-application /
    # line-choice profile aligns with the new grip level.
    "grip_level_change_magnitude_pct": 0.12,
    "laps_to_input_recalibration": 5,
}


def test_adaptation_rules_directory_loads_cleanly() -> None:
    """Every adaptation YAML must validate against RuleSpec. Pin: a
    malformed YAML or schema violation would crash the engine at
    startup; the loader must never raise on this path."""
    rules = load_rules(ADAPTATION_DIR)
    assert rules, "expected at least one adaptation rule"


def test_at_least_one_adaptation_rule_authored() -> None:
    """T151 acceptance: at least one rule. Block I sets the
    runway for 10 adaptation + conditions rules (T151-T160)."""
    rules = load_rules(ADAPTATION_DIR)
    assert len(rules) >= 1, (
        f"expected at least 1 adaptation rule, found "
        f"{len(rules)}: {[r.id for r in rules]}"
    )


def test_every_adaptation_rule_id_starts_with_ad_prefix() -> None:
    """Prefix collision check. The adaptation directory is the
    `ad.*` namespace — track / weather / setup / car / pressure
    response. A future contributor copying a `tm.*` / `fm.*` /
    `co.*` template into this dir without renaming the id would
    silently shadow another namespace via the loader's id-
    uniqueness check, and the rules engine would misroute
    evidence. Pin the namespacing convention."""
    rules = load_rules(ADAPTATION_DIR)
    bad = [r.id for r in rules if not r.id.startswith("ad.")]
    assert not bad, f"non-adaptation ids in adaptation dir: {bad}"


def test_every_adaptation_rule_is_in_strategy_category() -> None:
    """Adaptation patterns play out across multiple laps and
    span resource decisions (when to commit harder, when to pull
    back). Calling these `technique` would mis-rank them in the
    prescription queue (which separates strategic from tactical
    drills). Same categorization choice as the tire/fuel and
    endurance rules."""
    rules = load_rules(ADAPTATION_DIR)
    bad = [
        (r.id, r.category) for r in rules if r.category != "strategy"
    ]
    assert not bad, (
        f"adaptation rules with non-strategy category: {bad}"
    )


# -- T151: ad.grip_change.slow_to_adapt ------------------------


def test_grip_change_fires_on_combined_change_and_slow_adapt() -> None:
    """Both signals firing: grip-level change magnitude 12% (well
    above the 0.08 firing threshold — clear grip shift) AND 5
    laps to recalibrate inputs (above the 4-lap firing threshold
    — slow adaptation). Pin: the canonical "the track changed and
    you didn't" pattern produces an insight — the magnitude
    framing handles both grip increases (rubbering in, drying) and
    grip decreases (rain, degradation) with one threshold."""
    rules = load_rules(ADAPTATION_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "grip_level_change_magnitude_pct": 0.12,
            "laps_to_input_recalibration": 5,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _GRIP_CHANGE_RULE_ID in fired_ids, (
        f"expected {_GRIP_CHANGE_RULE_ID} to fire on combined "
        f"large-grip-change + slow-recalibration context, got "
        f"{sorted(fired_ids)}"
    )


def test_grip_change_silent_when_grip_stable() -> None:
    """Slow recalibration (5 laps) but grip-change magnitude only
    2% — the track is essentially stable, so "took 5 laps to find
    the brake markers" doesn't correspond to any real grip shift
    the driver should have adapted to. The slow recalibration is
    then setup-tuning or driver-warm-up, not slow-adaptation. The
    rule must NOT fire — coaching "adapt faster to grip changes"
    when the grip didn't change would mis-attribute the cause.
    Pin: the magnitude guard separates real grip-change scenarios
    from generic slow-warm-up scenarios (other rules cover those)."""
    rules = load_rules(ADAPTATION_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "grip_level_change_magnitude_pct": 0.02,
            "laps_to_input_recalibration": 5,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _GRIP_CHANGE_RULE_ID not in fired_ids, (
        f"{_GRIP_CHANGE_RULE_ID} fired on stable-grip context — "
        f"mis-attributed slow warm-up to a non-existent grip shift. "
        f"Fired: {sorted(fired_ids)}"
    )


def test_grip_change_silent_when_driver_adapted_promptly() -> None:
    """Clear grip-level change (12% magnitude — track rubbered in,
    or rain started) but driver recalibrated inputs in 1 lap. The
    driver is doing the right thing — fast adaptation is correct
    technique, not a coachable failure mode. The rule must NOT
    fire — coaching "adapt faster to grip changes" to a driver
    who already adapted in 1 lap would over-state the issue and
    waste their attention. Pin: the recalibration guard prevents
    firing on every grip-change event regardless of driver
    response. The rule targets the slow-adaptation failure mode
    specifically, not grip change in isolation."""
    rules = load_rules(ADAPTATION_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "grip_level_change_magnitude_pct": 0.12,
            "laps_to_input_recalibration": 1,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _GRIP_CHANGE_RULE_ID not in fired_ids, (
        f"{_GRIP_CHANGE_RULE_ID} fired on fast-adapting driver — "
        f"coached adaptation speed to a driver who already adapted "
        f"promptly. Fired: {sorted(fired_ids)}"
    )
