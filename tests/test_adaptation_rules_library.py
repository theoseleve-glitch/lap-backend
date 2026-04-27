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
_SETUP_REGRESSION_RULE_ID = "ad.setup_change.regression"
_WET_LINE_RULE_ID = "ad.weather_transition.wet_line_wrong"
_RAIN_ONSET_RULE_ID = "ad.rain_start.tire_commitment_late"
_TRAFFIC_DEFENSE_RULE_ID = "ad.traffic_loss.defensive_excess"
_NEW_TRACK_RULE_ID = "ad.new_track.learning_rate_slow"
_CAR_CHANGE_RULE_ID = "ad.car_change.transfer_gap"
_QUALI_RACE_DELTA_RULE_ID = "ad.qualifying_vs_race.pace_delta"


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
    # T152 — setup-change regression. Caller supplies the lap-time
    # regression in seconds (positive = slower after the setup
    # change vs the before-change baseline) and the count of clean
    # laps post-change confirming the regression isn't noise. The
    # rule fires when both hold — i.e., the change made the car
    # slower AND we have enough laps to know it's real, but the
    # driver/engineer kept the setup anyway.
    "setup_change_lap_time_regression_seconds": 0.35,
    "clean_lap_count_post_setup_change": 8,
    # T153 — weather-transition line choice. Caller supplies the
    # magnitude of track-wetness change between consecutive lap
    # windows (rain start, dry-out) and the pct of corners during
    # the transition where the driver was on the wrong line for
    # current conditions (dry line in wet → no rubber/no grip;
    # wet line in dry → off the racing groove). Distinct from
    # T151 (input recalibration) by surfacing the geometric
    # line-choice failure mode specifically.
    "weather_transition_magnitude_pct": 0.30,
    "pct_corners_on_wrong_line_for_conditions": 0.50,
    # T154 — rain-start commitment failure. Caller supplies the
    # rate of wetness increase per lap (rain coming in fast, grip
    # dropping rapidly) and the count of mistake events (lockups,
    # slides, offs) during the rain-onset window. Distinct from
    # T151 (broad slow-to-adapt) and T153 (wrong line) by surfacing
    # the specific mistake-event signature: driver kept pushing
    # dry-pace brake/throttle commitment into rapidly-dropping
    # wet grip, and the consequence shows up as concrete errors
    # rather than just lap-time deficit.
    "rain_onset_rate_pct_per_lap": 0.15,
    "mistake_event_count_in_rain_onset_window": 4,
    # T155 — traffic over-defending. Caller supplies the pct of
    # lap-distance where competitors are within close range
    # (real traffic situation, not clean-air running) and the
    # lap-time cost in seconds attributed to defensive line /
    # input choices vs the driver's own clean-air baseline. The
    # rule fires when both hold — real traffic AND the defensive
    # choices are costing meaningfully more than position
    # protection is worth. Distinct from `rc.defensive_line_too_often`
    # by being adaptation-driven: the failure mode is specifically
    # over-calibrating the defense response in traffic, not a
    # general defensive-line-frequency pattern.
    "traffic_proximity_pct_lap": 0.45,
    "defensive_lap_time_cost_seconds": 0.55,
    # T156 — new-track slow learning. Caller supplies the count
    # of prior sessions at this track (<=1 means genuinely new
    # to the driver, not a re-visit) and the driver's per-lap
    # improvement rate as a pct deficit below cohort norm
    # (>=0.30 means 30% below cohort improvement-per-lap; 0.50
    # means half cohort speed). Distinct from generic
    # consistency / pace rules by attributing the deficit
    # specifically to track-learning rate (vs setup, fitness,
    # or skill plateau).
    "prior_session_count_at_track": 0,
    "lap_time_improvement_per_lap_pct_below_cohort": 0.50,
    # T157 — car-change transfer gap. Caller supplies the count
    # of sessions since the driver switched cars (<=3 means
    # still in the typical transfer window) and the lap-time
    # deficit pct vs the driver's platform-normalized prior-car
    # baseline (>=0.02 means 2%+ slower than the cohort-adjusted
    # expectation, after correcting for car-class differences).
    # Distinct from T156 (track-learning) by attributing the
    # deficit specifically to car-platform transfer, not
    # track familiarity.
    "sessions_since_car_change": 1,
    "pace_transfer_gap_pct_above_baseline": 0.04,
    # T158 — qualifying vs race percentile drop. Caller supplies
    # the magnitude of the driver's cohort-percentile drop from
    # qualifying to race (>=0.20 means a 20-percentile drop, e.g.,
    # quali P10 / race P30 in a 50-car field) and the count of
    # clean race-session laps establishing the race-pace baseline.
    # Distinct from T156 (track-learning) and T157 (car-transfer)
    # by attributing specifically to the quali-aggression-vs-race-
    # commitment transfer failure mode.
    "qualifying_to_race_percentile_drop": 0.35,
    "clean_lap_count_in_race_session": 12,
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


# -- T152: ad.setup_change.regression --------------------------


def test_setup_regression_fires_on_combined_regression_and_enough_laps() -> None:
    """Both signals firing: lap-time regression of 0.35s vs
    pre-change baseline (well above the 0.20s firing threshold —
    meaningful regression, not lap-time noise) AND 8 clean laps
    post-change (well past the 5-lap minimum sample size needed
    to confirm the regression isn't a single-lap fluke or a tire-
    warm-up artifact). Pin: the canonical "the setup change made
    the car slower and you didn't revert it" pattern produces an
    insight — distinct from generic "you're slow today" because
    it attributes the slowdown to a specific reversible decision."""
    rules = load_rules(ADAPTATION_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "setup_change_lap_time_regression_seconds": 0.35,
            "clean_lap_count_post_setup_change": 8,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _SETUP_REGRESSION_RULE_ID in fired_ids, (
        f"expected {_SETUP_REGRESSION_RULE_ID} to fire on combined "
        f"large-regression + enough-laps context, got "
        f"{sorted(fired_ids)}"
    )


def test_setup_regression_silent_when_lap_count_too_low() -> None:
    """Regression magnitude of 0.35s but only 2 clean laps post-
    setup-change. A 2-lap sample is dominated by warm-up
    transients (cold tires, first-flying-lap concentration spike,
    track-evolution noise). The rule must NOT fire — sample size
    guard prevents premature regression diagnosis. Pin: the
    >=5 lap minimum keeps the post-change average meaningful;
    coaching "revert the setup" after 2 laps would chase noise."""
    rules = load_rules(ADAPTATION_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "setup_change_lap_time_regression_seconds": 0.35,
            "clean_lap_count_post_setup_change": 2,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _SETUP_REGRESSION_RULE_ID not in fired_ids, (
        f"{_SETUP_REGRESSION_RULE_ID} fired on 2-lap sample — coached "
        f"setup revert from a sample size where the regression is "
        f"warm-up noise. Fired: {sorted(fired_ids)}"
    )


def test_setup_regression_silent_when_change_neutral_or_improvement() -> None:
    """Plenty of clean laps (8) but the setup change improved
    lap time by 0.10s (regression value -0.10, well below the
    >=0.20 firing threshold). The change was good — coaching
    "revert" would undo a real improvement. The rule must NOT
    fire — coaching "your setup change made you slower" to a
    driver whose setup change made them faster is exactly
    backwards. Pin: the regression-magnitude guard prevents
    firing on every setup-change event regardless of direction.
    The rule targets the specific failure mode of regressions
    that weren't reverted, not setup changes in general."""
    rules = load_rules(ADAPTATION_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "setup_change_lap_time_regression_seconds": -0.10,
            "clean_lap_count_post_setup_change": 8,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _SETUP_REGRESSION_RULE_ID not in fired_ids, (
        f"{_SETUP_REGRESSION_RULE_ID} fired on improved-pace setup — "
        f"coached revert on a setup change that made the driver "
        f"faster. Fired: {sorted(fired_ids)}"
    )


# -- T153: ad.weather_transition.wet_line_wrong ----------------


def test_wet_line_fires_on_combined_transition_and_wrong_line() -> None:
    """Both signals firing: weather-transition magnitude 30% (well
    above the 0.15 firing threshold — clear wetness shift, e.g.,
    rain starting or track drying) AND 50% of corners on the
    wrong line for current conditions (well above the 0.30 firing
    threshold — driver isn't tracking the line that suits the
    new conditions). Pin: the canonical "the line moved and you
    didn't" pattern produces an insight, distinct from T151's
    input-recalibration framing by surfacing the geometric line-
    choice failure mode specifically — driver may have adapted
    brake/throttle markers but stayed on the dry-line groove
    after the rain came in (or the wet-line groove after dry-
    out)."""
    rules = load_rules(ADAPTATION_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "weather_transition_magnitude_pct": 0.30,
            "pct_corners_on_wrong_line_for_conditions": 0.50,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _WET_LINE_RULE_ID in fired_ids, (
        f"expected {_WET_LINE_RULE_ID} to fire on combined "
        f"large-transition + wrong-line context, got "
        f"{sorted(fired_ids)}"
    )


def test_wet_line_silent_when_weather_stable() -> None:
    """Driver on wrong line for 50% of corners but weather is
    stable (transition magnitude only 5% — within track-evolution
    noise, no actual rain start or dry-out). The wrong-line
    behavior is then attributable to driver line preference,
    setup-mismatch, or simply preferring a different reference
    line — not a weather-transition response failure. The rule
    must NOT fire — coaching "switch to the wet line" when there
    is no wet-line situation would mis-attribute the cause. Pin:
    the transition-magnitude guard separates weather-driven line-
    choice failures from generic line-discipline issues (covered
    by other rules)."""
    rules = load_rules(ADAPTATION_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "weather_transition_magnitude_pct": 0.05,
            "pct_corners_on_wrong_line_for_conditions": 0.50,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _WET_LINE_RULE_ID not in fired_ids, (
        f"{_WET_LINE_RULE_ID} fired on stable-weather context — "
        f"mis-attributed line-choice issue to a non-existent "
        f"weather transition. Fired: {sorted(fired_ids)}"
    )


def test_wet_line_silent_when_driver_already_on_correct_line() -> None:
    """Clear weather transition (30% wetness change — rain coming
    in or track drying) but driver only on wrong line for 10% of
    corners (well below the 0.30 firing threshold — driver moved
    to the right line for current conditions). The driver is
    doing the right thing — fast line adaptation is correct
    technique, not a coachable failure mode. The rule must NOT
    fire — coaching "switch to the correct line" to a driver
    already on the correct line would over-state the issue. Pin:
    the wrong-line guard prevents firing on every weather
    transition regardless of driver response. The rule targets
    the slow-line-adaptation failure mode specifically, not
    weather transitions in isolation."""
    rules = load_rules(ADAPTATION_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "weather_transition_magnitude_pct": 0.30,
            "pct_corners_on_wrong_line_for_conditions": 0.10,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _WET_LINE_RULE_ID not in fired_ids, (
        f"{_WET_LINE_RULE_ID} fired on driver already on correct "
        f"line — coached line-switching to a driver who already "
        f"adapted. Fired: {sorted(fired_ids)}"
    )


# -- T154: ad.rain_start.tire_commitment_late ------------------


def test_rain_onset_fires_on_combined_rain_rate_and_mistakes() -> None:
    """Both signals firing: rain coming in at 15% wetness per lap
    (well above the 0.08 firing threshold — clear rain start, not
    damp patches) AND 4 mistake events during the rain-onset
    window (well above the 3-event firing threshold — pattern,
    not one-off). Pin: the canonical "you kept pushing as the
    rain came in and paid for it in lockups/slides/offs" pattern
    produces an insight, distinct from T151's input-recalibration
    framing (broad lap-time deficit) and T153's geometric framing
    (wrong line). T154 surfaces the specific mistake-event
    signature of dry-pace commitment in dropping grip."""
    rules = load_rules(ADAPTATION_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "rain_onset_rate_pct_per_lap": 0.15,
            "mistake_event_count_in_rain_onset_window": 4,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _RAIN_ONSET_RULE_ID in fired_ids, (
        f"expected {_RAIN_ONSET_RULE_ID} to fire on combined "
        f"high-rain-rate + high-mistake-count context, got "
        f"{sorted(fired_ids)}"
    )


def test_rain_onset_silent_when_no_rain_start() -> None:
    """Plenty of mistakes (4) but rain-onset rate only 0.02
    (essentially stable conditions — no rain start, maybe damp
    patches at most). The mistakes are then attributable to
    driver-skill issues, cold tires, or setup — not commitment-
    vs-falling-wet-grip mismatch. The rule must NOT fire —
    coaching "pull back commitment for the rain" when there is
    no rain coming in would mis-attribute the cause and waste the
    operator's attention. Pin: the rain-onset-rate guard
    separates rain-start commitment failures from generic mistake
    patterns (covered by skill/setup rules)."""
    rules = load_rules(ADAPTATION_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "rain_onset_rate_pct_per_lap": 0.02,
            "mistake_event_count_in_rain_onset_window": 4,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _RAIN_ONSET_RULE_ID not in fired_ids, (
        f"{_RAIN_ONSET_RULE_ID} fired on stable-weather context — "
        f"mis-attributed mistake pattern to a non-existent rain "
        f"start. Fired: {sorted(fired_ids)}"
    )


def test_rain_onset_silent_when_driver_pulled_back_correctly() -> None:
    """Clear rain start (15% wetness per lap — grip dropping
    fast) but only 0 mistake events during the rain-onset window —
    the driver pulled back commitment immediately and avoided
    lockups, slides, and offs. The driver is doing the right
    thing — fast pullback is correct technique. The rule must
    NOT fire — coaching "pull back commitment for the rain" to a
    driver who already pulled back would over-state the issue
    and waste attention. Pin: the mistake-count guard prevents
    firing on every rain-onset event regardless of driver
    response. The rule targets the late-pullback failure mode
    specifically, not rain onset in isolation."""
    rules = load_rules(ADAPTATION_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "rain_onset_rate_pct_per_lap": 0.15,
            "mistake_event_count_in_rain_onset_window": 0,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _RAIN_ONSET_RULE_ID not in fired_ids, (
        f"{_RAIN_ONSET_RULE_ID} fired on driver who pulled back "
        f"correctly — coached commitment-pullback to a driver who "
        f"already adapted. Fired: {sorted(fired_ids)}"
    )


# -- T155: ad.traffic_loss.defensive_excess --------------------


def test_traffic_defense_fires_on_combined_traffic_and_defensive_cost() -> None:
    """Both signals firing: 45% of lap-distance in close-traffic
    range (well above the 0.30 firing threshold — real traffic,
    not clean-air running) AND 0.55s of lap-time attributed to
    defensive line/input choices vs clean-air baseline (well
    above the 0.40s firing threshold — meaningful cost, not
    noise). Pin: the canonical "you're defending harder than the
    position is worth" pattern produces an insight, distinct from
    `rc.defensive_line_too_often` which fires on overall
    defensive-line frequency. T155 is the traffic-specific cost-
    benefit framing."""
    rules = load_rules(ADAPTATION_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "traffic_proximity_pct_lap": 0.45,
            "defensive_lap_time_cost_seconds": 0.55,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _TRAFFIC_DEFENSE_RULE_ID in fired_ids, (
        f"expected {_TRAFFIC_DEFENSE_RULE_ID} to fire on combined "
        f"high-traffic + high-defensive-cost context, got "
        f"{sorted(fired_ids)}"
    )


def test_traffic_defense_silent_when_running_in_clean_air() -> None:
    """Defensive lap-time cost 0.55s recorded but only 5% of lap
    in close-traffic range — driver is mostly running in clean
    air, so any "defensive" lap-time cost is mis-labeled. The
    cost is then attributable to setup, line preference, or
    clean-air pace decay — not traffic-driven over-defending. The
    rule must NOT fire — coaching "defend less aggressively in
    traffic" when the driver isn't really in traffic would mis-
    attribute the cause. Pin: the traffic-proximity guard
    separates traffic-adaptation failures from clean-air pace
    issues (covered by other rules)."""
    rules = load_rules(ADAPTATION_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "traffic_proximity_pct_lap": 0.05,
            "defensive_lap_time_cost_seconds": 0.55,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _TRAFFIC_DEFENSE_RULE_ID not in fired_ids, (
        f"{_TRAFFIC_DEFENSE_RULE_ID} fired on clean-air context — "
        f"mis-attributed lap-time cost to a non-existent traffic "
        f"situation. Fired: {sorted(fired_ids)}"
    )


def test_traffic_defense_silent_when_defensive_cost_well_calibrated() -> None:
    """45% of lap in close-traffic range (real traffic) but
    defensive lap-time cost only 0.10s — the driver is defending
    appropriately, picking the cheap moments to defend and
    ceding gracefully where it costs too much. The rule must
    NOT fire — coaching "defend less" to a driver who is well-
    calibrated would over-correct and could cost track position
    needlessly. Pin: the defensive-cost guard prevents firing
    on every traffic situation regardless of how the defense is
    calibrated. The rule targets the specific over-defending
    failure mode where the protection cost exceeds the
    position-value benefit."""
    rules = load_rules(ADAPTATION_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "traffic_proximity_pct_lap": 0.45,
            "defensive_lap_time_cost_seconds": 0.10,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _TRAFFIC_DEFENSE_RULE_ID not in fired_ids, (
        f"{_TRAFFIC_DEFENSE_RULE_ID} fired on well-calibrated "
        f"defender — coached defensive-cost reduction to a driver "
        f"already calibrated. Fired: {sorted(fired_ids)}"
    )


# -- T156: ad.new_track.learning_rate_slow ---------------------


def test_new_track_fires_on_combined_new_track_and_slow_learning() -> None:
    """Both signals firing: 0 prior sessions at this track (well
    below the <=1 firing threshold — genuinely new track for
    this driver) AND per-lap improvement rate 50% below cohort
    norm (well above the 0.30 firing threshold — slow learning
    curve). Pin: the canonical "you're at a new track and not
    converging on baseline pace as fast as cohort peers" pattern
    produces an insight, distinct from generic consistency /
    pace rules by attributing the deficit specifically to
    track-learning rate."""
    rules = load_rules(ADAPTATION_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "prior_session_count_at_track": 0,
            "lap_time_improvement_per_lap_pct_below_cohort": 0.50,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _NEW_TRACK_RULE_ID in fired_ids, (
        f"expected {_NEW_TRACK_RULE_ID} to fire on combined "
        f"new-track + slow-learning context, got "
        f"{sorted(fired_ids)}"
    )


def test_new_track_silent_when_track_is_known() -> None:
    """Slow per-lap improvement (50% below cohort) but driver
    has 8 prior sessions at this track — they know the layout,
    so any improvement-rate deficit is attributable to skill
    plateau, setup, fitness, or other factors, not track
    learning. The rule must NOT fire — coaching "systematic
    track learning" to a driver who already knows the track
    would mis-attribute the cause and waste their attention.
    Pin: the new-track guard separates track-learning failures
    from generic plateau / pace issues (covered by other
    rules)."""
    rules = load_rules(ADAPTATION_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "prior_session_count_at_track": 8,
            "lap_time_improvement_per_lap_pct_below_cohort": 0.50,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _NEW_TRACK_RULE_ID not in fired_ids, (
        f"{_NEW_TRACK_RULE_ID} fired on known-track context — "
        f"mis-attributed plateau / pace issue to track learning. "
        f"Fired: {sorted(fired_ids)}"
    )


def test_new_track_silent_when_learning_rate_matches_cohort() -> None:
    """Genuinely new track (0 prior sessions) but per-lap
    improvement rate is on cohort norm (deficit only 0.05, well
    below the 0.30 firing threshold) — driver is converging on
    baseline at typical cohort speed. The rule must NOT fire —
    coaching "learn the track faster" to a driver already
    learning at cohort pace would over-state the issue. Pin: the
    learning-rate guard prevents firing on every new-track
    session regardless of convergence rate. The rule targets
    the slow-learning failure mode specifically, not new-track
    sessions in isolation."""
    rules = load_rules(ADAPTATION_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "prior_session_count_at_track": 0,
            "lap_time_improvement_per_lap_pct_below_cohort": 0.05,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _NEW_TRACK_RULE_ID not in fired_ids, (
        f"{_NEW_TRACK_RULE_ID} fired on cohort-paced learner — "
        f"coached track-learning to a driver already learning at "
        f"cohort speed. Fired: {sorted(fired_ids)}"
    )


# -- T157: ad.car_change.transfer_gap --------------------------


def test_car_change_fires_on_combined_recent_change_and_transfer_gap() -> None:
    """Both signals firing: 1 session since car change (well below
    the <=3 firing threshold — still in the typical transfer
    window) AND lap-time 4% above platform-normalized prior-car
    baseline (well above the 0.02 firing threshold — meaningful
    deficit, not noise). Pin: the canonical "you switched cars
    and the pace hasn't transferred" pattern produces an insight,
    distinct from T156 (track-learning) by attributing the
    deficit specifically to car-platform transfer."""
    rules = load_rules(ADAPTATION_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "sessions_since_car_change": 1,
            "pace_transfer_gap_pct_above_baseline": 0.04,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _CAR_CHANGE_RULE_ID in fired_ids, (
        f"expected {_CAR_CHANGE_RULE_ID} to fire on combined "
        f"recent-change + transfer-gap context, got "
        f"{sorted(fired_ids)}"
    )


def test_car_change_silent_when_well_settled_into_car() -> None:
    """4% pace deficit but driver has 12 sessions on the new car
    (well past the <=3 transfer window). The deficit is then
    attributable to car-class skill ceiling, setup, or pace
    plateau — not transfer adaptation. The rule must NOT fire —
    coaching "use the car-change transfer protocol" to a driver
    who has been on the car for months would mis-attribute the
    cause and waste their attention. Pin: the recency guard
    separates car-platform transfer failures from generic skill /
    setup issues at the new platform (covered by other rules)."""
    rules = load_rules(ADAPTATION_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "sessions_since_car_change": 12,
            "pace_transfer_gap_pct_above_baseline": 0.04,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _CAR_CHANGE_RULE_ID not in fired_ids, (
        f"{_CAR_CHANGE_RULE_ID} fired on settled-on-car driver — "
        f"mis-attributed plateau / skill issue to car-platform "
        f"transfer. Fired: {sorted(fired_ids)}"
    )


def test_car_change_silent_when_pace_transferred_cleanly() -> None:
    """Recent car change (1 session since switch) but pace deficit
    only 0.5% above prior-car baseline — driver transferred pace
    cleanly. The rule must NOT fire — coaching transfer-protocol
    discipline to a driver who already transferred well would
    over-state the issue. Pin: the transfer-gap guard prevents
    firing on every car-change event regardless of how the pace
    transferred. The rule targets the slow-transfer failure mode
    specifically, not car changes in isolation."""
    rules = load_rules(ADAPTATION_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "sessions_since_car_change": 1,
            "pace_transfer_gap_pct_above_baseline": 0.005,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _CAR_CHANGE_RULE_ID not in fired_ids, (
        f"{_CAR_CHANGE_RULE_ID} fired on clean-transfer driver — "
        f"coached transfer-protocol to a driver who already "
        f"transferred pace cleanly. Fired: {sorted(fired_ids)}"
    )


# -- T158: ad.qualifying_vs_race.pace_delta --------------------


def test_quali_race_delta_fires_on_combined_drop_and_enough_laps() -> None:
    """Both signals firing: 35-percentile drop from qualifying to
    race in cohort ranking (well above the 0.20 firing threshold —
    significant transfer failure, e.g., quali P10 → race P40+ in
    a 50-car field) AND 12 clean race laps (well past the 5-lap
    minimum sample size needed for race-pace baseline). Pin: the
    canonical "you can find one quick lap but can't sustain race
    pace" pattern produces an insight, distinct from T156 / T157
    by attributing specifically to quali-aggression-to-race-
    commitment transfer failure."""
    rules = load_rules(ADAPTATION_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "qualifying_to_race_percentile_drop": 0.35,
            "clean_lap_count_in_race_session": 12,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _QUALI_RACE_DELTA_RULE_ID in fired_ids, (
        f"expected {_QUALI_RACE_DELTA_RULE_ID} to fire on combined "
        f"high-drop + enough-laps context, got "
        f"{sorted(fired_ids)}"
    )


def test_quali_race_delta_silent_when_lap_count_too_low() -> None:
    """Big percentile drop (35%) but only 2 clean race laps —
    race-pace estimate is dominated by single-lap noise (one
    SC restart, one DRS-train lap, one error). The rule must
    NOT fire — sample size guard prevents diagnosing transfer
    failure from a noise-dominated race-pace estimate. Pin: the
    >=5 lap minimum keeps the race-pace baseline statistically
    meaningful for the percentile comparison."""
    rules = load_rules(ADAPTATION_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "qualifying_to_race_percentile_drop": 0.35,
            "clean_lap_count_in_race_session": 2,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _QUALI_RACE_DELTA_RULE_ID not in fired_ids, (
        f"{_QUALI_RACE_DELTA_RULE_ID} fired on 2-lap sample — "
        f"coached quali-to-race transfer from a sample size where "
        f"race pace is noise. Fired: {sorted(fired_ids)}"
    )


def test_quali_race_delta_silent_when_pace_transfers_cleanly() -> None:
    """12 clean race laps but percentile drop only 5% — the
    driver's race-pace ranking is essentially the same as their
    qualifying-pace ranking (e.g., quali P10 / race P11 in a
    50-car field). The rule must NOT fire — coaching "transfer
    quali aggression to race pace" to a driver who already
    transfers cleanly would over-state the issue. Pin: the drop-
    magnitude guard prevents firing on every multi-session
    weekend regardless of cross-format transfer quality. The
    rule targets the specific quali-strong / race-weak failure
    mode, not generic race performance."""
    rules = load_rules(ADAPTATION_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "qualifying_to_race_percentile_drop": 0.05,
            "clean_lap_count_in_race_session": 12,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _QUALI_RACE_DELTA_RULE_ID not in fired_ids, (
        f"{_QUALI_RACE_DELTA_RULE_ID} fired on clean-transfer "
        f"driver — coached quali-to-race transfer to a driver "
        f"already transferring cleanly. Fired: {sorted(fired_ids)}"
    )
