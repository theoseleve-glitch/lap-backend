"""T141+ (sprint2-backend) — tire / fuel management rules library.

Authors `coaching/library/rules/tire_fuel/` subtree; T141 lands the
first rule (`tm.tire_temp_overheat.aggressive_early`). Block H grows
this directory across T141-T150 (10 tire / fuel management rules).

Pinned behaviors:
  - directory loads cleanly (every YAML well-formed, RuleSpec-valid)
  - at least one tire/fuel rule exists (T141 acceptance)
  - every rule's id starts with `tm.` — tire/fuel management is
    its own namespace; the loader's id-uniqueness check catches
    cross-namespace collisions but the directory-level prefix
    guard catches a future copy-paste from `cs.*` / `co.*` / etc.
  - every rule is in the `strategy` category — tire/fuel are
    multi-lap / multi-stint resource management decisions, not
    single-corner skill execution
  - the early-overheat rule fires when both conditions hold:
    tire_temp_avg_above_optimal_first_3_laps_celsius >= 8.0 AND
    early_lap_throttle_application_pct_above_baseline >= 0.10
  - overheat-without-aggression keeps the rule silent — the
    failure mode is then setup or track temp, not driver-
    controllable. Coaching "be less aggressive" when the driver
    isn't being aggressive is wrong.
  - aggression-without-overheat keeps the rule silent — pushing
    early without burning the tires is fine; the rule targets
    the driver-attributable overheat failure mode specifically.

Mutation: dropping the temp guard would coach "save the tires"
to drivers whose tires aren't actually overheating. Dropping
the aggression guard would mis-attribute setup/temperature-
driven overheating to driver behavior.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from coaching.library.rule_loader import load_rules
from coaching.library.rule_runner import run_rules

REPO_ROOT = Path(__file__).resolve().parents[1]
TIRE_FUEL_DIR = REPO_ROOT / "coaching" / "library" / "rules" / "tire_fuel"

_EARLY_OVERHEAT_RULE_ID = "tm.tire_temp_overheat.aggressive_early"
_WEAR_IMBALANCE_RULE_ID = "tm.tire_wear.unbalanced_front_rear"
_TIRE_DECAY_RULE_ID = "tm.stint_pace_decay.tire_degradation"
_FLAT_SPOT_RULE_ID = "tm.lockup_pattern.flat_spot_risk"
_FUEL_BURN_EXCESS_RULE_ID = "fm.fuel_burn_excess.vs_target"


# Template vars referenced by the rule's explanations. Any
# firing-path test must carry these or the runner raises
# InsightRenderError.
_TEMPLATE_VARS: dict[str, Any] = {
    "tire_temp_avg_above_optimal_first_3_laps_celsius": 12.0,
    "early_lap_throttle_application_pct_above_baseline": 0.18,
    # T142 — front/rear wear-imbalance magnitude. Caller supplies
    # abs() of the (front - rear) wear-rate ratio; condition
    # compiler doesn't allow function calls (same pattern as
    # T134's magnitude framing). Magnitude framing covers both
    # front-heavy (over-trail-braking) and rear-heavy (over-
    # exit-aggression) failure modes with one threshold.
    "tire_wear_front_rear_imbalance_abs_pct": 0.30,
    "tire_wear_front_rear_imbalance_signed_pct": 0.30,
    "stint_lap_count": 12,
    # T143 — tire-degradation-attributed pace decay. REUSES T074's
    # `stint_pace_decay_seconds_per_lap` (when both fire, T143 is
    # the more-specific diagnosis) and adds an attribution guard:
    # caller supplies the fraction of decay that correlates with
    # measured tire wear. Distinct from T074 (cause-agnostic) and
    # T132 (driver-fade framing on lap_time alone).
    "stint_pace_decay_seconds_per_lap": 0.25,
    "stint_pace_decay_tire_attribution_pct": 0.85,
    # T144 — flat-spot risk from severe lockups. REUSES T192's
    # `lockup_event_count` and adds a severity guard: caller
    # supplies the avg severity index across all lockups. Mild
    # locks self-recover; severe locks scrub rubber and build
    # flat spots. T144 is the tire-consequence framing of T192's
    # braking-technique signal.
    "lockup_event_count": 4,
    "lockup_avg_severity_index": 0.65,
    # T145 — opens fuel-management `fm.*` namespace. Driver burning
    # more fuel per lap than the strategist's target. Caller
    # supplies the percentage excess vs target (avoids needing
    # division in the condition compiler).
    "fuel_burn_excess_pct_above_target": 0.08,
    "clean_lap_count_in_session": 8,
}


def test_tire_fuel_rules_directory_loads_cleanly() -> None:
    """Every tire/fuel YAML must validate against RuleSpec. Pin: a
    malformed YAML or schema violation would crash the engine at
    startup; the loader must never raise on this path."""
    rules = load_rules(TIRE_FUEL_DIR)
    assert rules, "expected at least one tire/fuel rule"


def test_at_least_one_tire_fuel_rule_authored() -> None:
    """T141 acceptance: at least one rule. Block H sets the
    runway for 10 tire/fuel rules (T141-T150)."""
    rules = load_rules(TIRE_FUEL_DIR)
    assert len(rules) >= 1, (
        f"expected at least 1 tire/fuel rule, found "
        f"{len(rules)}: {[r.id for r in rules]}"
    )


def test_every_tire_fuel_rule_id_starts_with_tm_or_fm_prefix() -> None:
    """Prefix collision check. The tire/fuel directory is shared
    by `tm.*` (tire management) and `fm.*` (fuel management) —
    both are multi-lap / multi-stint resource decisions. A
    future contributor copying a `cs.*` / `co.*` / `rc.*`
    template into this dir without renaming the id would
    silently shadow another namespace via the loader's id-
    uniqueness check, and the rules engine would misroute
    evidence. Pin the namespacing convention."""
    rules = load_rules(TIRE_FUEL_DIR)
    bad = [
        r.id for r in rules
        if not (r.id.startswith("tm.") or r.id.startswith("fm."))
    ]
    assert not bad, f"non-tire/fuel ids in tire/fuel dir: {bad}"


def test_every_tire_fuel_rule_is_in_strategy_category() -> None:
    """Tire/fuel management is multi-lap / multi-stint resource
    management. Calling these `technique` would mis-rank them
    in the prescription queue (which separates strategic from
    tactical drills). Same categorization choice as the
    endurance rules."""
    rules = load_rules(TIRE_FUEL_DIR)
    bad = [
        (r.id, r.category) for r in rules if r.category != "strategy"
    ]
    assert not bad, (
        f"tire/fuel rules with non-strategy category: {bad}"
    )


# -- T141: tm.tire_temp_overheat.aggressive_early --------------


def test_early_overheat_fires_on_combined_overheat_and_aggression() -> None:
    """Both signals firing: tires 12°C above optimal in the
    first 3 laps (well above the 8°C firing threshold —
    clear overheat) AND throttle inputs 18% above the driver's
    own baseline in the same window (well above the 0.10
    firing threshold — clear aggression). Pin: the canonical
    "you cooked the tires by pushing too hard before they
    stabilized" pattern produces an insight."""
    rules = load_rules(TIRE_FUEL_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "tire_temp_avg_above_optimal_first_3_laps_celsius": 12.0,
            "early_lap_throttle_application_pct_above_baseline": 0.18,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _EARLY_OVERHEAT_RULE_ID in fired_ids, (
        f"expected {_EARLY_OVERHEAT_RULE_ID} to fire on combined "
        f"overheat + aggression context, got "
        f"{sorted(fired_ids)}"
    )


def test_early_overheat_silent_when_aggression_absent() -> None:
    """Tires overheating early (12°C above optimal) but throttle
    application matches the driver's own baseline (no excess
    aggression). The overheat is then attributable to setup
    (too-soft compound, wrong pressures, hot ambient temps), not
    driver behavior. The rule must NOT fire — coaching "be less
    aggressive" to a driver who isn't being aggressive is wrong;
    the engineer should look at the setup. Pin: the aggression
    guard separates driver-controllable overheat from setup-
    driven overheat."""
    rules = load_rules(TIRE_FUEL_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "tire_temp_avg_above_optimal_first_3_laps_celsius": 12.0,
            "early_lap_throttle_application_pct_above_baseline": 0.02,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _EARLY_OVERHEAT_RULE_ID not in fired_ids, (
        f"{_EARLY_OVERHEAT_RULE_ID} fired on setup-driven overheat — "
        f"mis-attributed an engineering issue to driver behavior. "
        f"Fired: {sorted(fired_ids)}"
    )


def test_early_overheat_silent_when_temps_in_window() -> None:
    """Driver pushing aggressively (throttle 18% above baseline
    in the first 3 laps) but tire temps stay only 3°C above
    optimal — the tires absorb the extra workload without
    leaving the operating window. Could be cool ambient temps,
    a harder compound, or simply a driver whose aggression is
    well-modulated. The rule must NOT fire — coaching "save
    the tires" to a driver whose tires aren't overheating is
    noise. Pin: the temp guard prevents firing on every
    aggressive early-stint driver regardless of thermal
    outcome."""
    rules = load_rules(TIRE_FUEL_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "tire_temp_avg_above_optimal_first_3_laps_celsius": 3.0,
            "early_lap_throttle_application_pct_above_baseline": 0.18,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _EARLY_OVERHEAT_RULE_ID not in fired_ids, (
        f"{_EARLY_OVERHEAT_RULE_ID} fired on in-window-temp "
        f"driver — coached tire-saving to a driver whose tires "
        f"aren't overheating. Fired: {sorted(fired_ids)}"
    )


# -- T142: tm.tire_wear.unbalanced_front_rear ------------------


def test_wear_imbalance_fires_on_combined_imbalance_and_long_stint() -> None:
    """Both signals firing: front/rear wear imbalance magnitude
    30% (well above the 0.20 firing threshold — clear axle
    bias) AND 12 laps in the stint (well past the 5-lap
    minimum needed to compute meaningful wear measurements).
    Pin: the canonical "your driving style is loading one axle
    way more than the other" pattern produces an insight —
    diagnosis covers both front-heavy (over-trail-braking)
    and rear-heavy (over-exit-aggression) via magnitude framing."""
    rules = load_rules(TIRE_FUEL_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "tire_wear_front_rear_imbalance_abs_pct": 0.30,
            "stint_lap_count": 12,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _WEAR_IMBALANCE_RULE_ID in fired_ids, (
        f"expected {_WEAR_IMBALANCE_RULE_ID} to fire on combined "
        f"high-imbalance + long-stint context, got "
        f"{sorted(fired_ids)}"
    )


def test_wear_imbalance_silent_when_stint_too_short() -> None:
    """Imbalance magnitude 30% (would fire on a long stint) but
    only 3 laps in the stint. Wear measurements across 1-3 laps
    are dominated by warm-up transient effects (carcass temp
    swings, pressure ramp, surface scrubbing) — not steady-state
    wear pattern. The rule must NOT fire — sample size guard
    prevents diagnosing imbalance from out-lap noise. Pin: the
    >=5 lap minimum keeps wear-rate measurements steady-state."""
    rules = load_rules(TIRE_FUEL_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "tire_wear_front_rear_imbalance_abs_pct": 0.30,
            "stint_lap_count": 3,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _WEAR_IMBALANCE_RULE_ID not in fired_ids, (
        f"{_WEAR_IMBALANCE_RULE_ID} fired on 3-lap stint — coached "
        f"wear pattern from warm-up-transient noise. "
        f"Fired: {sorted(fired_ids)}"
    )


def test_wear_imbalance_silent_when_axles_balanced() -> None:
    """Long stint (12 laps) but imbalance only 5% — the driver's
    style loads both axles roughly evenly within the noise
    floor. The rule must NOT fire — coaching "you wear the
    fronts/rears too much" to a balanced driver is noise. Pin:
    the imbalance guard prevents firing on every long-stint
    driver regardless of axle bias. The rule targets the style-
    driven imbalance failure mode specifically, not stint
    length."""
    rules = load_rules(TIRE_FUEL_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "tire_wear_front_rear_imbalance_abs_pct": 0.05,
            "stint_lap_count": 12,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _WEAR_IMBALANCE_RULE_ID not in fired_ids, (
        f"{_WEAR_IMBALANCE_RULE_ID} fired on balanced-wear driver — "
        f"coached axle-bias to a driver loading both axles evenly. "
        f"Fired: {sorted(fired_ids)}"
    )


# -- T143: tm.stint_pace_decay.tire_degradation ----------------


def test_tire_decay_fires_on_combined_decay_and_high_tire_attribution() -> None:
    """Both signals firing: pace decay 0.25 s/lap (well above
    the 0.15 s/lap firing threshold — clear stint-pace decay,
    same threshold as T074) AND 85% of the decay correlated
    with measured tire wear (well above the 0.70 attribution
    threshold — decay is tire-driven, not driver-driven). Pin:
    the canonical "your stint pace falls off because the tires
    are degrading" pattern produces an insight that's distinct
    from T074 (cause-agnostic) and T132 (driver-fade)."""
    rules = load_rules(TIRE_FUEL_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "stint_pace_decay_seconds_per_lap": 0.25,
            "stint_pace_decay_tire_attribution_pct": 0.85,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _TIRE_DECAY_RULE_ID in fired_ids, (
        f"expected {_TIRE_DECAY_RULE_ID} to fire on combined "
        f"high-decay + high-tire-attribution context, got "
        f"{sorted(fired_ids)}"
    )


def test_tire_decay_silent_when_decay_below_threshold() -> None:
    """High tire attribution (85%) but pace decay only 0.05
    s/lap — the small decay that does happen is tire-driven,
    but it's within the noise floor and not coachable. The
    rule must NOT fire — coaching "manage your tires" to a
    driver whose stint pace is essentially flat is noise.
    Pin: the decay guard prevents firing on every tire-using
    stint regardless of decay magnitude."""
    rules = load_rules(TIRE_FUEL_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "stint_pace_decay_seconds_per_lap": 0.05,
            "stint_pace_decay_tire_attribution_pct": 0.85,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _TIRE_DECAY_RULE_ID not in fired_ids, (
        f"{_TIRE_DECAY_RULE_ID} fired on flat-pace stint — "
        f"coached tire management to a driver whose pace isn't "
        f"falling off. Fired: {sorted(fired_ids)}"
    )


def test_tire_decay_silent_when_decay_not_tire_attributable() -> None:
    """Clear pace decay (0.25 s/lap) but only 30% of it
    correlates with tire wear — the decay is mostly driver-
    fade or fuel-load (T132 territory), not tire degradation.
    The rule must NOT fire — coaching "your tires are
    degrading" when the decay is actually driver concentration
    drift would mis-prescribe the drill (T132's focus drill,
    not a tire-management drill). Pin: the attribution guard
    enforces T143's specific tire-degradation framing,
    distinct from T074's cause-agnostic decay diagnosis."""
    rules = load_rules(TIRE_FUEL_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "stint_pace_decay_seconds_per_lap": 0.25,
            "stint_pace_decay_tire_attribution_pct": 0.30,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _TIRE_DECAY_RULE_ID not in fired_ids, (
        f"{_TIRE_DECAY_RULE_ID} fired on driver-fade-attributed "
        f"decay — mis-prescribed tire-management drill for what "
        f"is a focus-discipline issue. Fired: {sorted(fired_ids)}"
    )


# -- T144: tm.lockup_pattern.flat_spot_risk --------------------


def test_flat_spot_fires_on_combined_lockup_count_and_severity() -> None:
    """Both signals firing: 4 lockup events (above the 3-event
    firing threshold — pattern, not one-off; same threshold
    as T192) AND avg severity 0.65 (above the 0.50 firing
    threshold — severe enough to scrub rubber, not mild self-
    recovering locks). Pin: the canonical "your lockups are
    accumulating into flat-spot risk" pattern produces an
    insight that's distinct from T192 (`br.lockup.pattern_detection`,
    which fires on count alone)."""
    rules = load_rules(TIRE_FUEL_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "lockup_event_count": 4,
            "lockup_avg_severity_index": 0.65,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _FLAT_SPOT_RULE_ID in fired_ids, (
        f"expected {_FLAT_SPOT_RULE_ID} to fire on combined "
        f"high-count + high-severity context, got "
        f"{sorted(fired_ids)}"
    )


def test_flat_spot_silent_when_lockups_are_mild() -> None:
    """Plenty of lockup events (4) but avg severity only 0.20 —
    mild brushes that self-recover within a few wheel
    rotations and don't scrub measurable rubber. T192 still
    fires (count-only), but T144 must NOT — coaching "you're
    flat-spotting your tires" when the lockups are too brief
    to leave marks would over-state the consequence and
    misroute the operator's attention away from T192's
    braking-technique fix. Pin: the severity guard enforces
    T144's flat-spot-risk framing, separate from T192's
    lockup-detection framing."""
    rules = load_rules(TIRE_FUEL_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "lockup_event_count": 4,
            "lockup_avg_severity_index": 0.20,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _FLAT_SPOT_RULE_ID not in fired_ids, (
        f"{_FLAT_SPOT_RULE_ID} fired on mild-lockup driver — "
        f"over-stated flat-spot risk for self-recovering brushes. "
        f"Fired: {sorted(fired_ids)}"
    )


def test_flat_spot_silent_when_lockup_count_too_low() -> None:
    """High severity (0.65) but only 1 lockup event — a single
    severe lockup is bad and worth attention via T192 (which
    fires from 3+ events), but a single event isn't a pattern
    of flat-spot accumulation. The rule must NOT fire — sample
    size guard prevents firing on one-off severe locks (e.g.,
    cold tire on out-lap, brake-balance miscalibration on first
    lap). Pin: the >=3 event minimum aligns with T192's
    detection threshold, requiring a pattern before flat-spot
    risk becomes a prescription priority."""
    rules = load_rules(TIRE_FUEL_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "lockup_event_count": 1,
            "lockup_avg_severity_index": 0.65,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _FLAT_SPOT_RULE_ID not in fired_ids, (
        f"{_FLAT_SPOT_RULE_ID} fired on 1-event sample — coached "
        f"flat-spot risk from an isolated severe lockup. "
        f"Fired: {sorted(fired_ids)}"
    )


# -- T145: fm.fuel_burn_excess.vs_target -----------------------


def test_fuel_burn_fires_on_combined_excess_and_enough_laps() -> None:
    """Both signals firing: 8% above target fuel-burn (well
    above the 0.05 firing threshold — meaningful excess) AND
    8 clean laps in the session (well past the 5-lap minimum
    sample size). Pin: the canonical "you're burning more fuel
    than the strategist budgeted" pattern produces an insight —
    distinct from T091 (preparation) and T104 (strategy
    execution score) by surfacing the per-lap fuel-burn signal
    specifically."""
    rules = load_rules(TIRE_FUEL_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "fuel_burn_excess_pct_above_target": 0.08,
            "clean_lap_count_in_session": 8,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _FUEL_BURN_EXCESS_RULE_ID in fired_ids, (
        f"expected {_FUEL_BURN_EXCESS_RULE_ID} to fire on combined "
        f"high-excess + enough-laps context, got "
        f"{sorted(fired_ids)}"
    )


def test_fuel_burn_silent_when_lap_count_too_low() -> None:
    """High excess (8% above target) but only 2 clean laps. A
    2-lap sample is dominated by single-lap noise — one
    aggressive lap, one DRS-train lap, one cold-tires push
    lap. The rule must NOT fire — sample size guard prevents
    firing on an unrepresentative slice. Pin: the >=5 lap
    minimum keeps the per-lap fuel-burn average meaningful."""
    rules = load_rules(TIRE_FUEL_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "fuel_burn_excess_pct_above_target": 0.08,
            "clean_lap_count_in_session": 2,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _FUEL_BURN_EXCESS_RULE_ID not in fired_ids, (
        f"{_FUEL_BURN_EXCESS_RULE_ID} fired on 2-lap sample — "
        f"coached fuel management from a sample size where the "
        f"average is noise. Fired: {sorted(fired_ids)}"
    )


def test_fuel_burn_silent_when_burn_on_target() -> None:
    """Plenty of laps (8) but excess only 1% — the driver is
    matching the strategist's fuel target within the noise
    floor. The rule must NOT fire — coaching "save fuel" to a
    driver already on-target is wrong; over-saving leaves
    pace on the table. Pin: the excess guard prevents firing
    on every multi-lap session regardless of fuel burn. The
    rule targets the over-burn failure mode specifically, not
    session length."""
    rules = load_rules(TIRE_FUEL_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "fuel_burn_excess_pct_above_target": 0.01,
            "clean_lap_count_in_session": 8,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _FUEL_BURN_EXCESS_RULE_ID not in fired_ids, (
        f"{_FUEL_BURN_EXCESS_RULE_ID} fired on on-target driver — "
        f"coached fuel-saving to a driver already matching the "
        f"strategist's target. Fired: {sorted(fired_ids)}"
    )
