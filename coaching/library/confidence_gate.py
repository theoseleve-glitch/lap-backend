"""T187 — confidence gating for fired rules.

Pure function. Decides whether a rule fires at FULL strength
(both evidence thresholds met) or merely TENTATIVE (one fired,
not enough corroboration). The runner / persistence layer
decides what to do with each level — surface a confident
insight, or note a tentative observation pending more data.

History (prior session dates) lives in the DB; this function
just decides given the count.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from enum import StrEnum

from coaching.library.rule_schema import RuleSpec


class ConfidenceLevel(StrEnum):
    FULL = "full"
    TENTATIVE = "tentative"


def evaluate_confidence(
    rule: RuleSpec,
    *,
    occurrences_in_session: int,
    prior_session_dates: Sequence[datetime],
    now: datetime,
) -> ConfidenceLevel:
    """Return FULL when both evidence thresholds are met, else
    TENTATIVE.

    The window is closed at both ends — a session exactly
    `window_days` ago counts. Half-open semantics would silently
    miss drivers right at the edge.
    """
    er = rule.detection.evidence_required
    if occurrences_in_session < er.min_occurrences_in_session:
        return ConfidenceLevel.TENTATIVE

    cutoff = now - timedelta(days=er.window_days)
    in_window = sum(1 for d in prior_session_dates if d >= cutoff)
    if in_window < er.min_sessions_in_window:
        return ConfidenceLevel.TENTATIVE

    return ConfidenceLevel.FULL
