"""T077 (sprint2-backend) — endurance-specific insight emission.

Integration test for the endurance rule library (T074) against a
synthetic stint fixture. The spec calls out an example: "your
pace decay in hour 2 is 0.8s/lap — coolant management issue".
The fixture (spa_24h_stint_2_thermal.yaml) materializes that
profile: pace decay 0.8 s/lap, tire preservation crashed to 0.30,
fuel burn 4.2 L/lap. Same shape as T200's braking integration
test against the Red Bull Ring fixture.

Pinned by these tests:
  - at least one rule fires (the spec acceptance: insights are
    "emitted")
  - the designed thermal-degradation set (pace decay + tire
    preservation + fuel burn) is the firing subset
  - non-fires stay non-fires (consistency + short-stint) — the
    rule library distinguishes problem profiles, not just labels
  - the rendered explanation for the pace-decay rule contains
    the stint index — the spec's "hour 2" cue
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from coaching.library.rule_loader import load_rules
from coaching.library.rule_runner import run_rules

LIBRARY_DIR = (
    Path(__file__).resolve().parents[1]
    / "coaching"
    / "library"
    / "rules"
)
ENDURANCE_DIR = LIBRARY_DIR / "endurance"
SESSION_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "coaching"
    / "sessions"
    / "spa_24h_stint_2_thermal.yaml"
)


def _load_session_context() -> dict[str, Any]:
    return yaml.safe_load(SESSION_FIXTURE.read_text(encoding="utf-8"))


def test_at_least_one_endurance_rule_fires_on_thermal_stint() -> None:
    """Sprint acceptance: insights ARE emitted on an endurance
    stint that shows real symptoms. A library that loads cleanly
    but never fires would silently fail this — pin the actual
    firing path."""
    rules = load_rules(ENDURANCE_DIR)
    insights = run_rules(rules, context=_load_session_context())
    assert insights, (
        f"expected ≥1 endurance fire on thermal-stint fixture, "
        f"got 0 (rules loaded: {[r.id for r in rules]})"
    )


def test_thermal_stint_fires_designed_three_rule_set() -> None:
    """Pin the firing subset: pace decay (the spec's "hour 2 =
    0.8s/lap" example), tire preservation, fuel burn. Threshold
    drift on any of these would silently shift the firing set
    and weaken endurance coaching."""
    rules = load_rules(ENDURANCE_DIR)
    insights = run_rules(rules, context=_load_session_context())
    fired_ids = {i.rule_id for i in insights}
    expected = {
        "enduro.stint_management.pace_decay_excessive",
        "enduro.tire.preservation_low",
        "enduro.fuel.burn_above_target",
    }
    assert expected.issubset(fired_ids), (
        f"expected {expected} ⊆ fired, got {fired_ids}"
    )


def test_thermal_stint_does_not_fire_consistency_or_short_stint() -> None:
    """The driver is consistently slow (not erratic) and the
    stint is full-length. Those rules must stay silent — if they
    fire here, either the fixture's signal values drifted or a
    threshold tuning regressed. Pin both negatives so adding a
    new rule doesn't accidentally lose the no-fire guarantees."""
    rules = load_rules(ENDURANCE_DIR)
    insights = run_rules(rules, context=_load_session_context())
    fired_ids = {i.rule_id for i in insights}
    assert (
        "enduro.stint_management.closing_stint_consistency_drop"
        not in fired_ids
    ), (
        "consistency-drop rule fired on a consistent (just slow) "
        "stint; check fixture's stint_closing_window_consistency_seconds"
    )
    assert (
        "enduro.stint_management.short_stint_unplanned" not in fired_ids
    ), (
        "short-stint rule fired on a 35-lap stint; check fixture's "
        "stint_lap_count"
    )


def test_pace_decay_explanation_carries_stint_index() -> None:
    """Spec example: "your pace decay in hour 2 is 0.8s/lap".
    The rendered insight must include the stint index so the
    driver knows WHICH stint the message is about. Without this
    interpolation, multi-stint sessions would surface ambiguous
    coaching ("you have decay" with no stint pointer)."""
    rules = load_rules(ENDURANCE_DIR)
    insights = run_rules(rules, context=_load_session_context())
    pace = next(
        i
        for i in insights
        if i.rule_id == "enduro.stint_management.pace_decay_excessive"
    )
    # Beginner template references {{stint_index}} → must render
    # the literal stint number, not the placeholder.
    assert "stint 2" in pace.explanation
    assert "{{" not in pace.explanation
