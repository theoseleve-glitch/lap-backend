"""T200 — integration test: 10 braking rules against synthetic
Red Bull Ring session.

Same shape as T198 (Sebring) and T199 (Silverstone). Red Bull
Ring is the third profile: short, twisty, big elevation changes
into Turn 1 (Niki-Lauda-Kurve) and Turn 3 (Remus). Distinct
mistake set from the other two:
- Late braking into T1 → apex speed deficit (T188, the
  universal symptom)
- Inconsistent pressure → elevation changes the brake reference
  point lap-to-lap, harder to anchor (T191)
- Rear-front slip imbalance → braking downhill into T1 unloads
  the rear, classic lockup signature (T195)

Designed firing set: T188 + T191 + T195. None of the three
overlap with Sebring's T192/T197 or Silverstone's T194/T196 —
the cross-fixture diffs are 4 rules each, which is the proof
the library distinguishes track *and* error profile, not just
labels things differently.
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
    / "red_bull_ring_amateur.yaml"
)


def _load_session_context() -> dict[str, Any]:
    return yaml.safe_load(SESSION_FIXTURE.read_text(encoding="utf-8"))


def test_at_least_three_rules_fire_on_red_bull_ring_session() -> None:
    """Sprint success criterion: meaningful insights = at least
    a handful of rules trigger on a representative session."""
    rules = load_rules(BRAKING_DIR)
    insights = run_rules(rules, context=_load_session_context())
    assert len(insights) >= 3, (
        f"expected ≥3 fires on Red Bull Ring fixture, got "
        f"{len(insights)}: {[i.rule_id for i in insights]}"
    )


def test_red_bull_ring_fixture_triggers_track_specific_set() -> None:
    """Pin the firing set so threshold drift becomes visible.
    These three are what the fixture is designed to surface for
    a typical Red Bull Ring amateur."""
    rules = load_rules(BRAKING_DIR)
    insights = run_rules(rules, context=_load_session_context())
    fired_ids = {i.rule_id for i in insights}
    expected_subset = {
        "br.late_braking.apex_speed_loss",
        "br.inconsistent_pressure.sector_variance",
        "br.balance_shift.rear_locking_entry",
    }
    assert expected_subset.issubset(fired_ids), (
        f"expected {expected_subset} ⊆ fired, got {fired_ids}"
    )


def test_red_bull_ring_fixture_does_not_fire_lockup_rule() -> None:
    """Smooth surface — bump-induced lockups don't apply. If
    T192 starts firing here, the fixture's lockup_event_count is
    wrong or T192's threshold drifted. (Rear lockups under
    braking-balance pressure are caught by T195 instead.)"""
    rules = load_rules(BRAKING_DIR)
    insights = run_rules(rules, context=_load_session_context())
    fired_ids = {i.rule_id for i in insights}
    assert "br.lockup.pattern_detection" not in fired_ids, (
        "lockup pattern rule fired on a smooth-surface fixture; "
        "balance-shift rule (T195) is the right diagnosis here"
    )


def test_red_bull_ring_firing_set_differs_from_sebring() -> None:
    """The three integration tests must surface different rule
    mixes per track. RBR vs Sebring should differ by at least 2
    rules in the symmetric difference."""
    sebring_path = (
        Path(__file__).parent
        / "fixtures"
        / "coaching"
        / "sessions"
        / "sebring_amateur.yaml"
    )
    rbr_ctx = _load_session_context()
    sebring_ctx = yaml.safe_load(
        sebring_path.read_text(encoding="utf-8")
    )

    rules = load_rules(BRAKING_DIR)
    rbr_fired = {i.rule_id for i in run_rules(rules, context=rbr_ctx)}
    sebring_fired = {
        i.rule_id for i in run_rules(rules, context=sebring_ctx)
    }
    differ = rbr_fired ^ sebring_fired
    assert len(differ) >= 2, (
        f"firing sets nearly identical — "
        f"red_bull_ring={rbr_fired}, sebring={sebring_fired}"
    )


def test_red_bull_ring_firing_set_differs_from_silverstone() -> None:
    """RBR vs Silverstone must also differ. If both this and the
    Sebring-vs-RBR test pass but Silverstone produces the same
    set as RBR, we've still got a coverage gap."""
    silverstone_path = (
        Path(__file__).parent
        / "fixtures"
        / "coaching"
        / "sessions"
        / "silverstone_amateur.yaml"
    )
    rbr_ctx = _load_session_context()
    silverstone_ctx = yaml.safe_load(
        silverstone_path.read_text(encoding="utf-8")
    )

    rules = load_rules(BRAKING_DIR)
    rbr_fired = {i.rule_id for i in run_rules(rules, context=rbr_ctx)}
    silverstone_fired = {
        i.rule_id for i in run_rules(rules, context=silverstone_ctx)
    }
    differ = rbr_fired ^ silverstone_fired
    assert len(differ) >= 2, (
        f"firing sets nearly identical — "
        f"red_bull_ring={rbr_fired}, silverstone={silverstone_fired}"
    )


def test_red_bull_ring_insights_carry_rendered_explanations() -> None:
    rules = load_rules(BRAKING_DIR)
    insights = run_rules(rules, context=_load_session_context())
    for insight in insights:
        assert insight.explanation
        assert "{{" not in insight.explanation
