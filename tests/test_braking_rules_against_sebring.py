"""T198 — integration test: all 10 braking rules against an
archived (synthetic) Sebring session.

Loads every rule under `coaching/library/rules/braking/` via the
loader (T182), runs them against the Sebring fixture context,
and verifies that at least 3 fire with sensible insights.

Acceptance: ≥3 fire — the success criterion baked into the
sprint task. The fixture is tuned so a typical amateur session
triggers 3 of the 10 rules (T188 late braking, T192 lockup,
T197 threshold under-driven) without firing the other 7. That
spread is the sanity check that the library isn't too lenient
(0 fires) or too aggressive (10 fires).
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
    / "sebring_amateur.yaml"
)


def _load_session_context() -> dict[str, Any]:
    return yaml.safe_load(SESSION_FIXTURE.read_text(encoding="utf-8"))


def test_all_ten_braking_rules_load_from_library() -> None:
    """Pre-condition: T188-T197 are present in the library and
    parse cleanly. If any are missing the integration assertion
    below would be testing fewer than 10 rules."""
    rules = load_rules(BRAKING_DIR)
    assert len(rules) == 10, (
        f"expected 10 braking rules, found {len(rules)}: "
        f"{sorted(r.id for r in rules)}"
    )


def test_at_least_three_rules_fire_on_sebring_session() -> None:
    """Sprint success criterion. Fewer than 3 means the library
    is too lenient for a session with multiple obvious mistakes."""
    rules = load_rules(BRAKING_DIR)
    insights = run_rules(rules, context=_load_session_context())
    assert len(insights) >= 3, (
        f"expected ≥3 fires on Sebring fixture, got {len(insights)}: "
        f"{[i.rule_id for i in insights]}"
    )


def test_sebring_fixture_triggers_expected_rules() -> None:
    """Pin the specific rules the fixture was designed to fire.
    Catches regressions where a future rule edit silently flips
    the firing set — e.g., a threshold tweak that makes T197
    silent on this session would be a real change in coaching
    behavior worth flagging."""
    rules = load_rules(BRAKING_DIR)
    insights = run_rules(rules, context=_load_session_context())
    fired_ids = {i.rule_id for i in insights}
    expected_subset = {
        "br.late_braking.apex_speed_loss",
        "br.lockup.pattern_detection",
        "br.threshold_braking.not_reached",
    }
    assert expected_subset.issubset(fired_ids), (
        f"expected {expected_subset} ⊆ fired, got {fired_ids}"
    )


def test_sebring_fixture_does_not_fire_consistency_rule() -> None:
    """Negative assertion — the fixture is meant to be a clean
    consistency story. If T191 starts firing here, the threshold
    drifted or the fixture's CoV value is wrong."""
    rules = load_rules(BRAKING_DIR)
    insights = run_rules(rules, context=_load_session_context())
    fired_ids = {i.rule_id for i in insights}
    assert (
        "br.inconsistent_pressure.sector_variance" not in fired_ids
    ), (
        "consistency rule fired on a clean-CoV fixture; check "
        "T191 threshold or fixture value"
    )


def test_sebring_insights_carry_rendered_explanations() -> None:
    """Insights from the integration must be presentable to a
    driver — explanation field non-empty and free of unrendered
    `{{placeholder}}` tokens."""
    rules = load_rules(BRAKING_DIR)
    insights = run_rules(rules, context=_load_session_context())
    for insight in insights:
        assert insight.explanation, (
            f"empty explanation on {insight.rule_id}"
        )
        assert "{{" not in insight.explanation, (
            f"unrendered placeholder in {insight.rule_id}: "
            f"{insight.explanation}"
        )
