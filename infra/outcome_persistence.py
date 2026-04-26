"""T219 — Outcome persistence wraps T218 evaluator into a write.
T220 — Propagates confidence change from outcome to episode.

Given an open prescription + an observed measurement + a
reference baseline, runs the success-condition evaluator and
persists the verdict:

    met / not_met → write ActionOutcome row, set
                    prescription.outcome_id, flip
                    prescription.status = 'completed';
                    move linked WeaknessEpisode.confidence by
                    -0.5 (met) or +0.1 (not_met), clamped [0,1]
    inconclusive  → write nothing, leave prescription and
                    episode alone

The unique constraint on `action_outcomes.prescription_id`
guarantees one outcome per prescription. Inconclusive returns
(None, "inconclusive") without touching the database.

confidence_change is the ACTUAL delta applied (after clamping),
not the nominal -0.5/+0.1 — so audit replays match. Episodes
with weakness_episode_id=None (born from standalone insights
per ADR-005) skip propagation; their outcome row carries
confidence_change=0.

prescription_efficacy_score is binary (1.0 met / 0.0 not_met)
for now. T-future will replace with graded scoring once we have
multiple outcomes per weakness to tune against.
"""

from datetime import datetime
from uuid import UUID

import uuid6
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from infra.orm import ActionOutcome, ActionPrescription, WeaknessEpisode
from infra.outcome_checker import OutcomeVerdict, evaluate_success_condition

_NOMINAL_CONFIDENCE_DELTA: dict[OutcomeVerdict, float] = {
    "met": -0.5,
    "not_met": 0.1,
    "inconclusive": 0.0,
}
_CONFIDENCE_MIN = 0.0
_CONFIDENCE_MAX = 1.0


def _efficacy_score(verdict: OutcomeVerdict) -> float:
    return 1.0 if verdict == "met" else 0.0


async def _propagate_confidence(
    db: AsyncSession,
    *,
    prescription: ActionPrescription,
    verdict: OutcomeVerdict,
) -> float:
    """Apply the verdict's nominal confidence delta to the linked
    episode (if any), clamped to [0, 1]. Returns the actual
    delta applied — what should be recorded on the outcome row."""
    if prescription.weakness_episode_id is None:
        return 0.0
    episode = (
        await db.execute(
            select(WeaknessEpisode).where(
                WeaknessEpisode.id == prescription.weakness_episode_id
            )
        )
    ).scalar_one_or_none()
    if episode is None:
        return 0.0
    nominal = _NOMINAL_CONFIDENCE_DELTA[verdict]
    new_conf = max(
        _CONFIDENCE_MIN,
        min(_CONFIDENCE_MAX, episode.confidence + nominal),
    )
    actual_change = new_conf - episode.confidence
    episode.confidence = new_conf
    return actual_change


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
    actual_confidence_change = await _propagate_confidence(
        db, prescription=prescription, verdict=verdict,
    )
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
            confidence_change=actual_confidence_change,
            prescription_efficacy_score=_efficacy_score(verdict),
        )
    )
    prescription.status = "completed"
    prescription.outcome_id = outcome_id
    await db.commit()
    return outcome_id, verdict
