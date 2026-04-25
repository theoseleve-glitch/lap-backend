from datetime import datetime
from uuid import UUID

import uuid6
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from infra.orm import ActionOutcome


class OutcomeRepo:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get(self, outcome_id: UUID) -> ActionOutcome | None:
        return await self._db.get(ActionOutcome, outcome_id)

    async def get_for_prescription(
        self, prescription_id: UUID
    ) -> ActionOutcome | None:
        result = await self._db.execute(
            select(ActionOutcome).where(
                ActionOutcome.prescription_id == prescription_id
            )
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        prescription_id: UUID,
        measured_at: datetime,
        success_condition_met: bool,
        measured_value: float,
        delta_vs_baseline: float,
        confidence_change: float,
        prescription_efficacy_score: float,
        session_ids_measured: list[str] | None = None,
        notes: str | None = None,
    ) -> ActionOutcome:
        outcome = ActionOutcome(
            id=uuid6.uuid7(),
            prescription_id=prescription_id,
            measured_at=measured_at,
            session_ids_measured=session_ids_measured or [],
            success_condition_met=success_condition_met,
            measured_value=measured_value,
            delta_vs_baseline=delta_vs_baseline,
            confidence_change=confidence_change,
            prescription_efficacy_score=prescription_efficacy_score,
            notes=notes,
        )
        self._db.add(outcome)
        await self._db.commit()
        return outcome
