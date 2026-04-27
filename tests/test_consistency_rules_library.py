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


# Template vars referenced by the rule's explanations. Any
# firing-path test must carry these or the runner raises
# InsightRenderError.
_TEMPLATE_VARS: dict[str, Any] = {
    "lap_time_stddev_seconds_in_session": 0.80,
    "clean_lap_count_in_session": 8,
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
