from typing import Any
from uuid import UUID

import uuid6
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from infra.orm import Session


class SessionRepo:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get(self, session_id: UUID) -> Session | None:
        return await self._db.get(Session, session_id)

    async def list_by_driver(self, driver_id: UUID) -> list[Session]:
        result = await self._db.execute(
            select(Session).where(Session.driver_id == driver_id)
        )
        return list(result.scalars().all())

    async def create(
        self,
        *,
        driver_id: UUID,
        manifest: dict[str, Any],
        status: str = "uploaded",
    ) -> Session:
        session = Session(
            id=uuid6.uuid7(),
            driver_id=driver_id,
            manifest=manifest,
            status=status,
        )
        self._db.add(session)
        await self._db.commit()
        await self._db.refresh(session)
        return session

    async def update_status(
        self, session_id: UUID, status: str
    ) -> Session | None:
        session = await self.get(session_id)
        if session is None:
            return None
        session.status = status
        await self._db.commit()
        await self._db.refresh(session)
        return session
