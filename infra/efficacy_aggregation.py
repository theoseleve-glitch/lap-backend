"""T221 — Efficacy score aggregation across three grains.

Three pure-compute queries over (action_outcomes,
action_prescriptions, weakness_episodes):

    efficacy_for_prescription → single prescription's outcome
    efficacy_for_driver       → mean across one driver's outcomes
    efficacy_for_rule         → mean across all drivers' outcomes
                                whose linked weakness used the
                                given library_rule_id

Per-prescription returns the row's score (or None if no outcome
exists yet — the prescription is still open). The driver/rule
variants return an EfficacyStat carrying sample_size so consumers
can decide whether to trust the mean.

Per-rule is the meta-signal across the whole user base — a low
mean across many drivers is evidence the rule itself is bad,
not that drivers are failing. Aggregations are SQL-side
(JOINs + AVG) so they stay cheap as outcome counts grow.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from infra.orm import ActionOutcome, ActionPrescription, WeaknessEpisode


@dataclass(frozen=True)
class EfficacyStat:
    sample_size: int
    mean_efficacy: float | None


async def efficacy_for_prescription(
    db: AsyncSession, prescription_id: UUID
) -> float | None:
    """Return the prescription's efficacy score, or None if no
    ActionOutcome row exists yet."""
    return (
        await db.execute(
            select(ActionOutcome.prescription_efficacy_score).where(
                ActionOutcome.prescription_id == prescription_id
            )
        )
    ).scalar_one_or_none()


def _stat_from_row(
    sample_size: int | None, mean: float | None
) -> EfficacyStat:
    if not sample_size:
        return EfficacyStat(sample_size=0, mean_efficacy=None)
    return EfficacyStat(
        sample_size=int(sample_size),
        mean_efficacy=float(mean) if mean is not None else None,
    )


async def efficacy_for_driver(
    db: AsyncSession, driver_id: UUID
) -> EfficacyStat:
    """Mean of prescription_efficacy_score across the driver's
    completed outcomes."""
    row = (
        await db.execute(
            select(
                func.count(ActionOutcome.id),
                func.avg(ActionOutcome.prescription_efficacy_score),
            )
            .join(
                ActionPrescription,
                ActionPrescription.id == ActionOutcome.prescription_id,
            )
            .where(ActionPrescription.driver_id == driver_id)
        )
    ).one()
    return _stat_from_row(row[0], row[1])


async def efficacy_for_rule(
    db: AsyncSession, library_rule_id: str
) -> EfficacyStat:
    """Mean of prescription_efficacy_score across ALL drivers'
    outcomes whose linked weakness episode used `library_rule_id`."""
    row = (
        await db.execute(
            select(
                func.count(ActionOutcome.id),
                func.avg(ActionOutcome.prescription_efficacy_score),
            )
            .join(
                ActionPrescription,
                ActionPrescription.id == ActionOutcome.prescription_id,
            )
            .join(
                WeaknessEpisode,
                WeaknessEpisode.id
                == ActionPrescription.weakness_episode_id,
            )
            .where(WeaknessEpisode.library_rule_id == library_rule_id)
        )
    ).one()
    return _stat_from_row(row[0], row[1])
