from datetime import datetime
from uuid import UUID

import uuid6
from sqlalchemy.ext.asyncio import AsyncSession

from infra.orm import WeaknessEpisode
from infra.posthog import PosthogClientProtocol


class WeaknessRepo:
    def __init__(
        self,
        db: AsyncSession,
        *,
        posthog_client: PosthogClientProtocol | None = None,
    ) -> None:
        self._db = db
        self._posthog = posthog_client

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
        # T173: emit `weakness_resolved` after the resolution is
        # durable. Closes the funnel started by T171's
        # `prescription_created` for this episode's drill template.
        if self._posthog is not None:
            self._posthog.capture(
                event="weakness_resolved",
                distinct_id=str(episode.driver_id),
                properties={
                    "episode_id": str(episode.id),
                    "category": episode.category,
                    "subcategory": episode.subcategory,
                    "library_rule_id": episode.library_rule_id,
                },
            )
        return episode
