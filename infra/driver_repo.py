from uuid import UUID

import uuid6
from sqlalchemy.ext.asyncio import AsyncSession

from infra.orm import DriverProfile


class DriverRepo:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get(self, driver_id: UUID) -> DriverProfile | None:
        return await self._db.get(DriverProfile, driver_id)

    async def create(
        self, *, user_id: UUID, display_name: str
    ) -> DriverProfile:
        driver = DriverProfile(
            id=uuid6.uuid7(),
            user_id=user_id,
            display_name=display_name,
        )
        self._db.add(driver)
        await self._db.commit()
        await self._db.refresh(driver)
        return driver

    async def update_settings(
        self,
        driver_id: UUID,
        *,
        display_name: str | None = None,
        skill_level: str | None = None,
        cohort_opt_in: bool | None = None,
    ) -> DriverProfile | None:
        driver = await self.get(driver_id)
        if driver is None:
            return None
        if display_name is not None:
            driver.display_name = display_name
        if skill_level is not None:
            driver.skill_level = skill_level
        if cohort_opt_in is not None:
            driver.cohort_opt_in = cohort_opt_in
        await self._db.commit()
        await self._db.refresh(driver)
        return driver
