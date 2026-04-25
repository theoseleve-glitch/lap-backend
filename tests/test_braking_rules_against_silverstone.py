"""T199 — integration test: 10 braking rules against synthetic
Silverstone session.

Same shape as T198 (Sebring) — different track, different
fixture, different expected firing set. Silverstone is fast and
smooth: no bump-induced lockups (T192 silent), well-driven
threshold (T197 silent). The mistakes show up in mid-corner
behavior on the long fast sweepers.

Designed firing set: T188 (late braking into Stowe), T194
(over-braking past apex at Vale), T196 (trail brake released
too early into Maggots/Becketts). Three different rules from
the Sebring set — proof that the library distinguishes track
profiles rather than firing the same insights everywhere.
"""

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
BRAKING_DIR = LIBRARY_DIR / "braking"
SESSION_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "coaching"
    / "sessions"
    / "silverstone_amateur.yaml"
)


def _load_session_context() -> dict[str, Any]:
    return yaml.safe_load(SESSION_FIXTURE.read_text(encoding="utf-8"))


def test_at_least_three_rules_fire_on_silverstone_session() -> None:
    """Sprint success criterion: meaningful insights = at least
    a handful of rules trigger on a representative session."""
    rules = load_rules(BRAKING_DIR)
    insights = run_rules(rules, context=_load_session_context())
    assert len(insights) >= 3, (
        f"expected ≥3 fires on Silverstone fixture, got "
        f"{len(insights)}: {[i.rule_id for i in insights]}"
    )


def test_silverstone_fixture_triggers_track_specific_set() -> None:
    """Pin the firing set so threshold drift becomes visible.
    These three are what the fixture is designed to surface for
    a typical Silverstone amateur."""
    rules = load_rules(BRAKING_DIR)
    insights = run_rules(rules, context=_load_session_context())
    fired_ids = {i.rule_id for i in insights}
    expected_subset = {
        "br.late_braking.apex_speed_loss",
        "br.over_braking.mid_corner",
        "br.trail_brake.release_too_early",
    }
    assert expected_subset.issubset(fired_ids), (
        f"expected {expected_subset} ⊆ fired, got {fired_ids}"
    )


def test_silverstone_fixture_does_not_fire_lockup_rule() -> None:
    """Smooth surface — bump-induced lockups don't apply. If
    T192 starts firing here, the fixture's lockup_event_count
    is wrong or T192's threshold drifted."""
    rules = load_rules(BRAKING_DIR)
    insights = run_rules(rules, context=_load_session_context())
    fired_ids = {i.rule_id for i in insights}
    assert "br.lockup.pattern_detection" not in fired_ids, (
        "lockup rule fired on a smooth-surface fixture"
    )


def test_silverstone_firing_set_differs_from_sebring() -> None:
    """The two integration tests must surface different rule
    mixes. If both tracks produce identical insights, the
    library has coverage gaps (the same 3 rules dominate
    everywhere, the others are dead branches)."""
    sebring_path = (
        Path(__file__).parent
        / "fixtures"
        / "coaching"
        / "sessions"
        / "sebring_amateur.yaml"
    )
    silverstone_ctx = _load_session_context()
    sebring_ctx = yaml.safe_load(
        sebring_path.read_text(encoding="utf-8")
    )

    rules = load_rules(BRAKING_DIR)
    silverstone_fired = {
        i.rule_id for i in run_rules(rules, context=silverstone_ctx)
    }
    sebring_fired = {
        i.rule_id for i in run_rules(rules, context=sebring_ctx)
    }
    # Symmetric difference: at least 2 rules differ between the
    # tracks (1 unique to each). Otherwise the diagnoses are
    # too track-agnostic to be useful.
    differ = silverstone_fired ^ sebring_fired
    assert len(differ) >= 2, (
        f"firing sets nearly identical — "
        f"silverstone={silverstone_fired}, sebring={sebring_fired}"
    )


def test_silverstone_insights_carry_rendered_explanations() -> None:
    rules = load_rules(BRAKING_DIR)
    insights = run_rules(rules, context=_load_session_context())
    for insight in insights:
        assert insight.explanation
        assert "{{" not in insight.explanation
