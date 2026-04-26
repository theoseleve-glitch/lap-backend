"""T218 — Outcome checker (pure evaluator).

Given a prescription's `success_condition` dict, an observed
metric value, and a reference baseline, returns a verdict:

    "met"          — observed/reference ratio satisfies the operator
    "not_met"      — observed/reference ratio fails the operator
    "inconclusive" — comparison can't be made (reference is 0 or
                     non-positive, so the ratio is undefined)

The function is intentionally currency-free: target_pct is a
ratio, ratios in mean nothing without a denominator, so we
compute observed/reference and let the operator decide. This
keeps the YAML rule library reusable across metrics with wildly
different units (km/h, count, percentage, seconds).

T219 wraps this in the persistence pipeline (compute observed
from session aggregates, look up reference from prior baseline,
write ActionOutcome row, flip prescription.status). Keeping the
evaluator pure means T219 can replay deterministically and the
operator can sanity-check verdicts in isolation.
"""

from typing import Any, Literal

OutcomeVerdict = Literal["met", "not_met", "inconclusive"]


def evaluate_success_condition(
    *,
    condition: dict[str, Any],
    observed_value: float,
    reference_value: float,
) -> OutcomeVerdict:
    """Score one prescription's success_condition. Returns one of
    'met' / 'not_met' / 'inconclusive'."""
    if reference_value <= 0:
        return "inconclusive"
    ratio = observed_value / reference_value
    target = float(condition["target_pct"])
    comparison = condition["comparison"]
    if comparison == "gte":
        return "met" if ratio >= target else "not_met"
    if comparison == "gt":
        return "met" if ratio > target else "not_met"
    if comparison == "lte":
        return "met" if ratio <= target else "not_met"
    if comparison == "lt":
        return "met" if ratio < target else "not_met"
    if comparison == "eq":
        return "met" if ratio == target else "not_met"
    raise ValueError(f"unknown comparison operator: {comparison!r}")
