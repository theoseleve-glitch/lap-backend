"""T218 — Outcome checker (pure evaluator).

Evaluates a prescription's `success_condition` against an
observed metric value relative to a reference baseline. Returns
one of three verdicts:

    met            — the condition is satisfied
    not_met        — observed value falls outside the target
    inconclusive   — the comparison can't be made (reference 0)

T218 is just the evaluator. T219 wraps it: load each open
prescription, compute its observed/reference values from the
session, run this function, persist a verdict + flip the
prescription status.

The contract is intentionally currency-free: target_pct is a
ratio, observed_value/reference_value is a ratio, the
comparison operator decides which way it goes. No metric units
are baked in.
"""

import pytest

from infra.outcome_checker import evaluate_success_condition


def test_gte_met_when_ratio_at_or_above_target() -> None:
    """0.96 target → 96% recovery is enough; 95% is not."""
    assert (
        evaluate_success_condition(
            condition={
                "metric": "apex_speed",
                "comparison": "gte",
                "target_pct": 0.96,
                "measurement_window": "next_3_sessions",
            },
            observed_value=96.0,
            reference_value=100.0,
        )
        == "met"
    )


def test_gte_not_met_when_ratio_below_target() -> None:
    assert (
        evaluate_success_condition(
            condition={
                "metric": "apex_speed",
                "comparison": "gte",
                "target_pct": 0.96,
                "measurement_window": "next_3_sessions",
            },
            observed_value=95.0,
            reference_value=100.0,
        )
        == "not_met"
    )


def test_gt_requires_strictly_greater_than() -> None:
    """Hitting exactly target_pct is NOT met for strict >."""
    assert (
        evaluate_success_condition(
            condition={
                "metric": "x", "comparison": "gt",
                "target_pct": 1.0, "measurement_window": "w",
            },
            observed_value=100.0,
            reference_value=100.0,
        )
        == "not_met"
    )


def test_lte_met_when_ratio_at_or_below_target() -> None:
    """For 'lower is better' metrics like brake_lockup_count."""
    assert (
        evaluate_success_condition(
            condition={
                "metric": "lockup_count", "comparison": "lte",
                "target_pct": 0.5, "measurement_window": "w",
            },
            observed_value=5.0,
            reference_value=10.0,
        )
        == "met"
    )


def test_lt_requires_strictly_less_than() -> None:
    assert (
        evaluate_success_condition(
            condition={
                "metric": "x", "comparison": "lt",
                "target_pct": 1.0, "measurement_window": "w",
            },
            observed_value=100.0,
            reference_value=100.0,
        )
        == "not_met"
    )


def test_eq_met_on_exact_match() -> None:
    assert (
        evaluate_success_condition(
            condition={
                "metric": "x", "comparison": "eq",
                "target_pct": 1.0, "measurement_window": "w",
            },
            observed_value=100.0,
            reference_value=100.0,
        )
        == "met"
    )


def test_inconclusive_when_reference_is_zero() -> None:
    """Division by zero → no signal possible → inconclusive,
    not 'met' (which would falsely signal success)."""
    assert (
        evaluate_success_condition(
            condition={
                "metric": "x", "comparison": "gte",
                "target_pct": 0.96, "measurement_window": "w",
            },
            observed_value=10.0,
            reference_value=0.0,
        )
        == "inconclusive"
    )


def test_inconclusive_when_reference_is_negative() -> None:
    """Negative reference is meaningless for ratio comparison."""
    assert (
        evaluate_success_condition(
            condition={
                "metric": "x", "comparison": "gte",
                "target_pct": 0.96, "measurement_window": "w",
            },
            observed_value=10.0,
            reference_value=-1.0,
        )
        == "inconclusive"
    )


def test_unknown_comparison_raises() -> None:
    """Defense-in-depth — Pydantic should already prevent this
    upstream, but if a malformed dict slips through we want a
    loud failure, not a silent 'inconclusive'."""
    with pytest.raises(ValueError, match="comparison"):
        evaluate_success_condition(
            condition={
                "metric": "x", "comparison": "approximately",
                "target_pct": 0.96, "measurement_window": "w",
            },
            observed_value=10.0,
            reference_value=10.0,
        )
