"""T219 — Outcome persistence wraps T218 evaluator into a write.

Given an open prescription + an observed measurement + a
reference baseline, runs the success-condition evaluator and
persists the verdict:

    met / not_met → write ActionOutcome row, set
                    prescription.outcome_id, flip
                    prescription.status = 'completed'
    inconclusive  → write nothing, leave prescription alone

The unique constraint on `action_outcomes.prescription_id`
guarantees one outcome per prescription. Inconclusive returns
(None, "inconclusive") without touching the database, so the
caller can re-run later when more data lands without worrying
about double-write.

confidence_change and prescription_efficacy_score get placeholder
values for now (binary efficacy: 1.0 met / 0.0 not_met,
confidence_change=0). T-future will replace these with proper
decay/Bayesian updates once we have multiple outcomes per
weakness to tune against.
"""

from datetime import datetime
from uuid import UUID

import uuid6
from sqlalchemy.ext.asyncio import AsyncSession

from infra.orm import ActionOutcome, ActionPrescription
from infra.outcome_checker import OutcomeVerdict, evaluate_success_condition

_PLACEHOLDER_CONFIDENCE_CHANGE = 0.0


def _efficacy_score(verdict: OutcomeVerdict) -> float:
    return 1.0 if verdict == "met" else 0.0


async def persist_outcome(
    db: AsyncSession,
    *,
    prescription: ActionPrescription,
    measured_value: float,
    reference_value: float,
    session_ids_measured: list[str],
    measured_at: datetime,
) -> tuple[UUID | None, OutcomeVerdict]:
    """Score the prescription's success_condition; on a definitive
    verdict, write an ActionOutcome row and complete the
    prescription. Inconclusive returns (None, 'inconclusive')
    without persisting anything."""
    verdict = evaluate_success_condition(
        condition=prescription.success_condition,
        observed_value=measured_value,
        reference_value=reference_value,
    )
    if verdict == "inconclusive":
        return None, verdict
    outcome_id = uuid6.uuid7()
    db.add(
        ActionOutcome(
            id=outcome_id,
            prescription_id=prescription.id,
            measured_at=measured_at,
            session_ids_measured=session_ids_measured,
            success_condition_met=(verdict == "met"),
            measured_value=measured_value,
            delta_vs_baseline=measured_value - reference_value,
            confidence_change=_PLACEHOLDER_CONFIDENCE_CHANGE,
            prescription_efficacy_score=_efficacy_score(verdict),
        )
    )
    prescription.status = "completed"
    prescription.outcome_id = outcome_id
    await db.commit()
    return outcome_id, verdict
