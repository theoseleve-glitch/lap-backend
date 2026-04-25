from datetime import datetime
from uuid import UUID

import uuid6
from sqlalchemy.ext.asyncio import AsyncSession

from infra.orm import WeaknessEpisode


class WeaknessRepo:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def open(
        self,
        *,
        driver_id: UUID,
        category: str,
        subcategory: str,
        severity: str,
        confidence: float,
        library_rule_id: str,
        first_observed_at: datetime,
        last_observed_at: datetime,
        recurrence_count: int,
    ) -> WeaknessEpisode:
        episode = WeaknessEpisode(
            id=uuid6.uuid7(),
            driver_id=driver_id,
            category=category,
            subcategory=subcategory,
            severity=severity,
            confidence=confidence,
            library_rule_id=library_rule_id,
            first_observed_at=first_observed_at,
            last_observed_at=last_observed_at,
            recurrence_count=recurrence_count,
            status="open",
        )
        self._db.add(episode)
        await self._db.commit()
        return episode

    async def update_confidence(
        self, episode_id: UUID, confidence: float
    ) -> WeaknessEpisode | None:
        episode = await self._db.get(WeaknessEpisode, episode_id)
        if episode is None:
            return None
        episode.confidence = confidence
        await self._db.commit()
        return episode

    async def resolve(
        self, episode_id: UUID, *, resolved_at: datetime
    ) -> WeaknessEpisode | None:
        episode = await self._db.get(WeaknessEpisode, episode_id)
        if episode is None:
            return None
        episode.status = "resolved"
        episode.resolved_at = resolved_at
        await self._db.commit()
        return episode
