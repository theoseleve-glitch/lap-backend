"""T131+ (sprint2-backend) — consistency / racecraft rules library.

Authors `coaching/library/rules/consistency/` subtree; T131 lands the
first rule (`co.lap_time_variance.high_spread`). Block H grows
this directory across T131-T140 (10 consistency / racecraft rules).

Pinned behaviors:
  - directory loads cleanly (every YAML well-formed, RuleSpec-valid)
  - at least one consistency rule exists (T131 acceptance)
  - every rule's id starts with `co.` or `rc.` — consistency vs
    racecraft are sister namespaces in this directory; the loader's
    id-uniqueness check catches collisions, but the directory-level
    prefix guard catches a future copy-paste from `cs.*` or `th.*`
  - every rule is in the `technique` category — consistency and
    racecraft are single-driver / single-session skill execution,
    not multi-session strategy
  - the high-spread rule fires when both conditions hold:
    lap_time_stddev_seconds_in_session >= 0.50 AND
    clean_lap_count_in_session >= 3
  - lap-count too low keeps the rule silent — stddev across 1-2
    laps is statistical noise, not a coachable inconsistency
  - low stddev keeps the rule silent even with many clean laps —
    a smooth driver with 20 clean laps is not inconsistent

Mutation: dropping the count guard would coach "you're inconsistent"
to a driver who only put in 2 clean laps (where stddev is meaningless).
Dropping the stddev guard would fire on every consistent driver who
ran enough clean laps. Both conjuncts are load-bearing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from coaching.library.rule_loader import load_rules
from coaching.library.rule_runner import run_rules

REPO_ROOT = Path(__file__).resolve().parents[1]
CONSISTENCY_DIR = REPO_ROOT / "coaching" / "library" / "rules" / "consistency"

_HIGH_SPREAD_RULE_ID = "co.lap_time_variance.high_spread"
_FADE_OVER_RUN_RULE_ID = "co.lap_time_trend.fade_over_run"
_T1_VOLATILE_RULE_ID = "co.sector_consistency.t1_volatile"
_BRAKE_DRIFT_RULE_ID = "co.braking_point_drift.over_run"
_PASS_ABORT_RULE_ID = "rc.passing_attempts.abort_rate"


# Template vars referenced by the rule's explanations. Any
# firing-path test must carry these or the runner raises
# InsightRenderError.
_TEMPLATE_VARS: dict[str, Any] = {
    "lap_time_stddev_seconds_in_session": 0.80,
    "clean_lap_count_in_session": 8,
    # T132 — lap-time trend slope (positive = getting slower).
    # Separate signal from T074's `stint_pace_decay_seconds_per_lap`
    # (which is strategy/tire-framing); this one is driver-fade
    # framing across a single run.
    "lap_time_trend_slope_seconds_per_lap": 0.18,
    # T133 — sector-1 localized-variance signals. Distinct from
    # T131 (session-wide spread) by requiring the variance to
    # be concentrated in S1 (share guard), not session-wide.
    "sector_1_lap_time_stddev_seconds": 0.25,
    "sector_1_stddev_share_of_total": 0.65,
    # T134 — brake-point drift magnitude (caller supplies abs()
    # of the brake-point slope; condition compiler doesn't allow
    # function calls — see ADR-condition-compiler whitelist).
    # Magnitude framing covers both creep-earlier and drift-later
    # rhythm losses with a single threshold.
    "brake_point_drift_magnitude_meters_per_lap": 0.80,
    "brake_point_drift_signed_meters_per_lap": 0.80,
    # T135 — opens racecraft `rc.*` namespace. Multi-driver
    # tactical signals. abort_rate = fraction of passing
    # attempts that the driver initiated and then bailed out
    # of mid-move.
    "passing_attempt_count": 8,
    "passing_attempt_abort_rate": 0.55,
}


def test_consistency_rules_directory_loads_cleanly() -> None:
    """Every consistency YAML must validate against RuleSpec. Pin: a
    malformed YAML or schema violation would crash the engine at
    startup; the loader must never raise on this path."""
    rules = load_rules(CONSISTENCY_DIR)
    assert rules, "expected at least one consistency rule"


def test_at_least_one_consistency_rule_authored() -> None:
    """T131 acceptance: at least one rule. Block H sets the
    runway for 10 consistency / racecraft rules (T131-T140)."""
    rules = load_rules(CONSISTENCY_DIR)
    assert len(rules) >= 1, (
        f"expected at least 1 consistency rule, found "
        f"{len(rules)}: {[r.id for r in rules]}"
    )


def test_every_consistency_rule_id_starts_with_co_or_rc_prefix() -> None:
    """Prefix collision check. The consistency directory is shared
    by `co.*` (consistency) and `rc.*` (racecraft) — both are
    single-driver session-level technique. A future contributor
    copying a `cs.*` or `th.*` template into this dir without
    renaming the id would silently shadow another namespace via
    the loader's id-uniqueness check, and the rules engine would
    misroute evidence. Pin the namespacing convention."""
    rules = load_rules(CONSISTENCY_DIR)
    bad = [
        r.id for r in rules
        if not (r.id.startswith("co.") or r.id.startswith("rc."))
    ]
    assert not bad, f"non-consistency/racecraft ids in dir: {bad}"


def test_every_consistency_rule_is_in_technique_category() -> None:
    """Consistency and racecraft are single-driver / single-session
    skill execution, not multi-session strategy. Calling these
    `strategy` would mis-rank them in the prescription queue
    (which separates strategic from tactical drills)."""
    rules = load_rules(CONSISTENCY_DIR)
    bad = [
        (r.id, r.category) for r in rules if r.category != "technique"
    ]
    assert not bad, (
        f"consistency rules with non-technique category: {bad}"
    )


# -- T131: co.lap_time_variance.high_spread --------------------


def test_high_spread_fires_on_combined_high_stddev_and_enough_laps() -> None:
    """Both signals firing: lap-time stddev 0.80s (well above the
    0.50s firing threshold — clearly inconsistent) AND 8 clean
    laps in the session (well past the 3-lap minimum sample size).
    Pin: the canonical "you can drive fast but you can't
    repeat it" pattern produces an insight."""
    rules = load_rules(CONSISTENCY_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "lap_time_stddev_seconds_in_session": 0.80,
            "clean_lap_count_in_session": 8,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _HIGH_SPREAD_RULE_ID in fired_ids, (
        f"expected {_HIGH_SPREAD_RULE_ID} to fire on combined "
        f"high-stddev + enough-laps context, got "
        f"{sorted(fired_ids)}"
    )


def test_high_spread_silent_when_lap_count_too_low() -> None:
    """High stddev (0.80s) but only 2 clean laps in the session.
    Stddev across 1-2 laps is statistical noise, not a coachable
    inconsistency — a single bad lap (traffic, mistake, brake-
    point miss) would dominate the spread. The rule must NOT
    fire — sample size guard prevents firing on noise. Pin: the
    >=3 lap minimum prevents premature inconsistency
    diagnosis."""
    rules = load_rules(CONSISTENCY_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "lap_time_stddev_seconds_in_session": 0.80,
            "clean_lap_count_in_session": 2,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _HIGH_SPREAD_RULE_ID not in fired_ids, (
        f"{_HIGH_SPREAD_RULE_ID} fired on 2-lap session — coached "
        f"inconsistency from a sample size where stddev is noise. "
        f"Fired: {sorted(fired_ids)}"
    )


def test_high_spread_silent_when_driver_consistent() -> None:
    """Low stddev (0.15s) across 8 clean laps — a smooth, repeating
    driver who isn't inconsistent. The rule must NOT fire —
    coaching "you're inconsistent" to a consistent driver is
    noise. Pin: the stddev guard prevents firing on every driver
    who happened to put in 3+ clean laps."""
    rules = load_rules(CONSISTENCY_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "lap_time_stddev_seconds_in_session": 0.15,
            "clean_lap_count_in_session": 8,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _HIGH_SPREAD_RULE_ID not in fired_ids, (
        f"{_HIGH_SPREAD_RULE_ID} fired on consistent driver — "
        f"coached inconsistency to a smooth/repeating driver. "
        f"Fired: {sorted(fired_ids)}"
    )


# -- T132: co.lap_time_trend.fade_over_run ---------------------


def test_fade_over_run_fires_on_combined_positive_slope_and_long_run() -> None:
    """Both signals firing: lap-time slope +0.18s/lap (clearly
    getting slower) AND 8 clean laps in the run (well past the
    5-lap minimum needed to compute a meaningful trend). Pin:
    the canonical "you fade over a stint" pattern produces an
    insight — distinct from T074's tire-decay framing because
    this is a driver-fatigue / concentration-drift diagnosis,
    not a strategy / tire-management diagnosis."""
    rules = load_rules(CONSISTENCY_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "lap_time_trend_slope_seconds_per_lap": 0.18,
            "clean_lap_count_in_session": 8,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _FADE_OVER_RUN_RULE_ID in fired_ids, (
        f"expected {_FADE_OVER_RUN_RULE_ID} to fire on combined "
        f"positive-slope + long-run context, got "
        f"{sorted(fired_ids)}"
    )


def test_fade_over_run_silent_when_run_too_short() -> None:
    """Slope +0.18s/lap (would fire on a long run) but only 4
    clean laps. A 4-lap trend is dominated by single-lap noise —
    one bad lap at the end gives a false positive slope. The
    rule must NOT fire — sample size guard prevents premature
    fade diagnosis. Pin: the >=5 lap minimum keeps trend slopes
    statistically meaningful."""
    rules = load_rules(CONSISTENCY_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "lap_time_trend_slope_seconds_per_lap": 0.18,
            "clean_lap_count_in_session": 4,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _FADE_OVER_RUN_RULE_ID not in fired_ids, (
        f"{_FADE_OVER_RUN_RULE_ID} fired on 4-lap run — coached "
        f"fade from a sample size where slope is noise. "
        f"Fired: {sorted(fired_ids)}"
    )


def test_fade_over_run_silent_when_pace_holding() -> None:
    """Long run (8 clean laps) but slope only +0.02s/lap — the
    driver is holding pace across the run (within noise floor).
    The rule must NOT fire — coaching "you're fading" to a
    driver who's actually holding pace is noise. Pin: the slope
    guard prevents firing on every long run regardless of trend.
    Negative-slope drivers (getting faster as they warm up) are
    also covered by the >=0.10 firing threshold."""
    rules = load_rules(CONSISTENCY_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "lap_time_trend_slope_seconds_per_lap": 0.02,
            "clean_lap_count_in_session": 8,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _FADE_OVER_RUN_RULE_ID not in fired_ids, (
        f"{_FADE_OVER_RUN_RULE_ID} fired on pace-holding driver — "
        f"coached fade to a driver actually holding pace. "
        f"Fired: {sorted(fired_ids)}"
    )


# -- T133: co.sector_consistency.t1_volatile -------------------


def test_t1_volatile_fires_on_combined_high_stddev_and_concentration() -> None:
    """Both signals firing: S1 stddev 0.25s (above the 0.15s
    floor — S1 is meaningfully spread) AND S1 contributing 65%
    of the total session variance (above the 0.50 share floor —
    variance is concentrated in S1, not session-wide). Pin: the
    canonical "S1 specifically is your weak sector" pattern
    produces an insight, distinct from T131's session-wide
    diagnosis."""
    rules = load_rules(CONSISTENCY_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "sector_1_lap_time_stddev_seconds": 0.25,
            "sector_1_stddev_share_of_total": 0.65,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _T1_VOLATILE_RULE_ID in fired_ids, (
        f"expected {_T1_VOLATILE_RULE_ID} to fire on combined "
        f"high-S1-stddev + S1-concentrated context, got "
        f"{sorted(fired_ids)}"
    )


def test_t1_volatile_silent_when_variance_is_session_wide() -> None:
    """High S1 stddev (0.25s) but S1 share only 0.34 (variance
    is roughly evenly distributed across S1, S2, S3). This is
    session-wide spread — T131's domain, not T133's. The rule
    must NOT fire — coaching "fix S1" when the driver is
    inconsistent everywhere mis-prioritizes the drill. Pin: the
    share guard enforces T133's localized-in-S1 framing."""
    rules = load_rules(CONSISTENCY_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "sector_1_lap_time_stddev_seconds": 0.25,
            "sector_1_stddev_share_of_total": 0.34,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _T1_VOLATILE_RULE_ID not in fired_ids, (
        f"{_T1_VOLATILE_RULE_ID} fired on session-wide variance — "
        f"mis-attributed session-wide spread to S1. "
        f"Fired: {sorted(fired_ids)}"
    )


def test_t1_volatile_silent_when_absolute_stddev_too_small() -> None:
    """S1 contributes 65% of total variance (concentrated in S1)
    but S1 absolute stddev only 0.05s — the absolute variance is
    in the noise floor. A driver where S1 dominates the (tiny)
    total variance is a non-issue. The rule must NOT fire —
    coaching "S1 is volatile" when the absolute spread is 0.05s
    is wasted attention. Pin: the absolute floor prevents firing
    on driver-level noise that happens to concentrate in S1."""
    rules = load_rules(CONSISTENCY_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "sector_1_lap_time_stddev_seconds": 0.05,
            "sector_1_stddev_share_of_total": 0.65,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _T1_VOLATILE_RULE_ID not in fired_ids, (
        f"{_T1_VOLATILE_RULE_ID} fired on noise-floor S1 stddev — "
        f"coached a non-issue. "
        f"Fired: {sorted(fired_ids)}"
    )


# -- T134: co.braking_point_drift.over_run ---------------------


def test_brake_drift_fires_on_combined_high_magnitude_and_long_run() -> None:
    """Both signals firing: brake-point drift magnitude 0.80m/lap
    (well above the 0.50m/lap firing threshold — clear rhythm
    loss) AND 8 clean laps in the run (well past the 5-lap
    minimum needed to compute a meaningful trend). Pin: the
    canonical "your brake points are drifting across the run"
    pattern produces an insight. Distinct from T132 (lap-time
    fade) by surfacing the channel-level cause — brake-point
    creep — so the drill is brake-point-anchor, not generic
    focus."""
    rules = load_rules(CONSISTENCY_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "brake_point_drift_magnitude_meters_per_lap": 0.80,
            "clean_lap_count_in_session": 8,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _BRAKE_DRIFT_RULE_ID in fired_ids, (
        f"expected {_BRAKE_DRIFT_RULE_ID} to fire on combined "
        f"high-magnitude + long-run context, got "
        f"{sorted(fired_ids)}"
    )


def test_brake_drift_silent_when_run_too_short() -> None:
    """Drift magnitude 0.80m/lap (would fire on a long run) but
    only 4 clean laps. A 4-lap brake-point trend is dominated
    by single-lap noise — one missed brake point inflates the
    slope. The rule must NOT fire — sample size guard prevents
    premature drift diagnosis. Pin: the >=5 lap minimum keeps
    brake-point trends statistically meaningful."""
    rules = load_rules(CONSISTENCY_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "brake_point_drift_magnitude_meters_per_lap": 0.80,
            "clean_lap_count_in_session": 4,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _BRAKE_DRIFT_RULE_ID not in fired_ids, (
        f"{_BRAKE_DRIFT_RULE_ID} fired on 4-lap run — coached "
        f"brake-point drift from a sample size where slope is "
        f"noise. Fired: {sorted(fired_ids)}"
    )


def test_brake_drift_silent_when_brake_points_locked() -> None:
    """Long run (8 clean laps) but drift magnitude only 0.10m/lap —
    the driver's brake points are essentially locked across the
    run (within noise floor). The rule must NOT fire — coaching
    "your brake points drift" to a driver hitting the same brake
    point every lap is noise. Pin: the magnitude guard prevents
    firing on every long run regardless of brake-point trend."""
    rules = load_rules(CONSISTENCY_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "brake_point_drift_magnitude_meters_per_lap": 0.10,
            "clean_lap_count_in_session": 8,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _BRAKE_DRIFT_RULE_ID not in fired_ids, (
        f"{_BRAKE_DRIFT_RULE_ID} fired on locked-brake-point "
        f"driver — coached drift to a driver with consistent "
        f"brake points. Fired: {sorted(fired_ids)}"
    )


# -- T135: rc.passing_attempts.abort_rate ----------------------


def test_pass_abort_fires_on_combined_high_rate_and_enough_attempts() -> None:
    """Both signals firing: 55% of passing attempts aborted (well
    above the 0.40 firing threshold — clear pattern of bailing
    out mid-move) AND 8 attempts in the session (well past the
    5-attempt minimum sample size). Pin: the canonical "you
    commit to passes you don't finish" pattern produces an
    insight — the racecraft diagnosis is decision-making, not
    pace."""
    rules = load_rules(CONSISTENCY_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "passing_attempt_count": 8,
            "passing_attempt_abort_rate": 0.55,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _PASS_ABORT_RULE_ID in fired_ids, (
        f"expected {_PASS_ABORT_RULE_ID} to fire on combined "
        f"high-abort-rate + enough-attempts context, got "
        f"{sorted(fired_ids)}"
    )


def test_pass_abort_silent_when_attempt_count_too_low() -> None:
    """High abort rate (0.55) but only 3 attempts in the session.
    A 1-of-3 or 2-of-3 abort rate is single-event noise — one
    bad gap selection inflates the rate. The rule must NOT fire —
    sample size guard prevents premature racecraft diagnosis.
    Pin: the >=5 attempt minimum keeps the rate statistically
    meaningful."""
    rules = load_rules(CONSISTENCY_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "passing_attempt_count": 3,
            "passing_attempt_abort_rate": 0.55,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _PASS_ABORT_RULE_ID not in fired_ids, (
        f"{_PASS_ABORT_RULE_ID} fired on 3-attempt sample — coached "
        f"racecraft from a sample size where rate is noise. "
        f"Fired: {sorted(fired_ids)}"
    )


def test_pass_abort_silent_when_completion_rate_healthy() -> None:
    """Plenty of attempts (8) but abort rate only 0.20 (driver
    completes 80% of moves they commit to). The rule must NOT
    fire — coaching "you bail out" to a driver who's converting
    most attempts is noise. Pin: the rate guard prevents firing
    on every active racer regardless of conversion. Healthy
    aggression with high conversion is exactly what we want; the
    rule targets the "starts-but-doesn't-finish" failure mode
    specifically."""
    rules = load_rules(CONSISTENCY_DIR)
    insights = run_rules(
        rules,
        context={
            **_TEMPLATE_VARS,
            "passing_attempt_count": 8,
            "passing_attempt_abort_rate": 0.20,
        },
    )
    fired_ids = {ins.rule_id for ins in insights}
    assert _PASS_ABORT_RULE_ID not in fired_ids, (
        f"{_PASS_ABORT_RULE_ID} fired on healthy-conversion "
        f"driver — coached pass-abort behavior to a driver "
        f"converting 80% of moves. Fired: {sorted(fired_ids)}"
    )
