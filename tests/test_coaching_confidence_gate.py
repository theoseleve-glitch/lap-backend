"""T187 — confidence gating: only fire at full strength after N
corroborating sessions.

A rule firing once on a single lap could be a fluke (driver
sneezed mid-corner). Gating waits for corroboration:
  - in-session: at least `min_occurrences_in_session` matches
  - across-sessions: at least `min_sessions_in_window` distinct
    sessions in the last `window_days` days where the rule fired
Both thresholds met → FULL confidence. Either short → TENTATIVE.

Pure function: history lives in the DB, the gate just decides
given the count.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from coaching.library.confidence_gate import (
    ConfidenceLevel,
    evaluate_confidence,
)
from coaching.library.rule_schema import RuleSpec

ADR_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "coaching"
    / "adr_013_example.yaml"
)


def _adr_rule() -> RuleSpec:
    raw: dict[str, Any] = yaml.safe_load(
        ADR_FIXTURE.read_text(encoding="utf-8")
    )
    return RuleSpec.model_validate(raw)


def test_full_confidence_when_both_thresholds_met() -> None:
    rule = _adr_rule()  # min_occ=3, min_sessions=2, window=14
    now = datetime(2026, 4, 26, tzinfo=UTC)
    prior = [now - timedelta(days=1), now - timedelta(days=5)]
    level = evaluate_confidence(
        rule,
        occurrences_in_session=3,
        prior_session_dates=prior,
        now=now,
    )
    assert level == ConfidenceLevel.FULL


def test_tentative_when_below_occurrence_threshold() -> None:
    rule = _adr_rule()
    now = datetime(2026, 4, 26, tzinfo=UTC)
    prior = [now - timedelta(days=1), now - timedelta(days=5)]
    level = evaluate_confidence(
        rule,
        occurrences_in_session=2,  # need 3
        prior_session_dates=prior,
        now=now,
    )
    assert level == ConfidenceLevel.TENTATIVE


def test_tentative_when_below_session_threshold() -> None:
    rule = _adr_rule()
    now = datetime(2026, 4, 26, tzinfo=UTC)
    prior = [now - timedelta(days=1)]  # only 1 prior session
    level = evaluate_confidence(
        rule,
        occurrences_in_session=5,
        prior_session_dates=prior,
        now=now,
    )
    assert level == ConfidenceLevel.TENTATIVE


def test_old_prior_sessions_outside_window_do_not_count() -> None:
    """A driver who hasn't raced in a month resets to zero —
    stale corroborations shouldn't keep firing insights about
    technique they may have already fixed."""
    rule = _adr_rule()  # window_days=14
    now = datetime(2026, 4, 26, tzinfo=UTC)
    prior = [
        now - timedelta(days=20),  # outside window
        now - timedelta(days=30),  # outside window
    ]
    level = evaluate_confidence(
        rule,
        occurrences_in_session=10,
        prior_session_dates=prior,
        now=now,
    )
    assert level == ConfidenceLevel.TENTATIVE


def test_exact_threshold_counts_as_full() -> None:
    """Boundary: 3 ≥ 3 and 2 ≥ 2."""
    rule = _adr_rule()
    now = datetime(2026, 4, 26, tzinfo=UTC)
    prior = [
        now - timedelta(days=2),
        now - timedelta(days=10),
    ]
    level = evaluate_confidence(
        rule,
        occurrences_in_session=3,
        prior_session_dates=prior,
        now=now,
    )
    assert level == ConfidenceLevel.FULL


def test_empty_prior_sessions_is_tentative() -> None:
    """First-ever observation of this rule for the driver →
    tentative. They might be a fluke."""
    rule = _adr_rule()
    now = datetime(2026, 4, 26, tzinfo=UTC)
    level = evaluate_confidence(
        rule,
        occurrences_in_session=10,
        prior_session_dates=[],
        now=now,
    )
    assert level == ConfidenceLevel.TENTATIVE


def test_mixed_in_and_out_of_window_counts_only_in_window() -> None:
    rule = _adr_rule()
    now = datetime(2026, 4, 26, tzinfo=UTC)
    prior = [
        now - timedelta(days=3),  # in
        now - timedelta(days=20),  # out
        now - timedelta(days=10),  # in
        now - timedelta(days=100),  # out
    ]
    level = evaluate_confidence(
        rule,
        occurrences_in_session=3,
        prior_session_dates=prior,
        now=now,
    )
    # 2 in-window prior sessions, occurrences ≥ 3 → FULL.
    assert level == ConfidenceLevel.FULL


def test_window_boundary_includes_dates_exactly_at_edge() -> None:
    """A session exactly `window_days` ago should count — half-open
    intervals where the boundary excludes will silently miss
    drivers right at the edge."""
    rule = _adr_rule()  # window=14
    now = datetime(2026, 4, 26, tzinfo=UTC)
    prior = [
        now - timedelta(days=14),  # exactly at boundary
        now - timedelta(days=2),
    ]
    level = evaluate_confidence(
        rule,
        occurrences_in_session=3,
        prior_session_dates=prior,
        now=now,
    )
    assert level == ConfidenceLevel.FULL
