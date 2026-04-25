from datetime import datetime
from typing import Any
from uuid import UUID

import uuid6
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from infra.orm import ActionPrescription


class PrescriptionRepo:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get(self, prescription_id: UUID) -> ActionPrescription | None:
        return await self._db.get(ActionPrescription, prescription_id)

    async def list_by_driver(
        self, driver_id: UUID
    ) -> list[ActionPrescription]:
        result = await self._db.execute(
            select(ActionPrescription).where(
                ActionPrescription.driver_id == driver_id
            )
        )
        return list(result.scalars().all())

    async def create(
        self,
        *,
        driver_id: UUID,
        created_at: datetime,
        priority: str,
        title: str,
        rationale: str,
        expected_context: dict[str, Any],
        success_condition: dict[str, Any],
        status: str,
        weakness_episode_id: UUID | None = None,
        due_before_session_id: UUID | None = None,
        valid_until: datetime | None = None,
        drill_template_id: str | None = None,
        accepted_at: datetime | None = None,
        outcome_id: UUID | None = None,
    ) -> ActionPrescription:
        pres = ActionPrescription(
            id=uuid6.uuid7(),
            driver_id=driver_id,
            weakness_episode_id=weakness_episode_id,
            created_at=created_at,
            priority=priority,
            due_before_session_id=due_before_session_id,
            valid_until=valid_until,
            title=title,
            rationale=rationale,
            drill_template_id=drill_template_id,
            expected_context=expected_context,
            success_condition=success_condition,
            status=status,
            accepted_at=accepted_at,
            outcome_id=outcome_id,
        )
        self._db.add(pres)
        await self._db.commit()
        return pres

    async def update_status(
        self, prescription_id: UUID, status: str
    ) -> ActionPrescription | None:
        pres = await self._db.get(ActionPrescription, prescription_id)
        if pres is None:
            return None
        pres.status = status
        await self._db.commit()
        return pres
