"""T209 — nightly WeaknessEpisode confidence decay.

Per ADR-005 §5.2, every WeaknessEpisode carries `confidence` in
[0, 1] that decays at `confidence_decay_per_day` per day without
re-observation. T208's merge resets the clock by advancing
`last_observed_at`.

The job recomputes confidence from
    1.0 - decay * (now - last_observed_at_in_days)
clamped to [0, 1]. Idempotent: two runs same `now` produce the
same final state, so a missed nightly tick is harmless — the
next run catches up.

Only `status="open"` rows are processed. Resolved/dormant
episodes keep their historical confidence.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from infra.orm import WeaknessEpisode

_OPEN_STATUS = "open"
_SECONDS_PER_DAY = 86400.0
_TOLERANCE = 1e-9


def decay_confidence(*, decay_per_day: float, days: float) -> float:
    """Apply the decay formula. Pure — testable without a DB.
    Result clamped to [0, 1] so the stored column never goes
    negative or supernatural."""
    raw = 1.0 - decay_per_day * days
    return max(0.0, min(1.0, raw))


async def decay_open_episodes(
    db: AsyncSession, *, now: datetime
) -> int:
    """Recompute confidence for every open episode based on
    days-since-last_observed_at. Returns the number of rows
    whose stored confidence actually moved (so an operator can
    distinguish a real run from a no-op on a quiet day)."""
    rows = (
        await db.execute(
            select(WeaknessEpisode).where(
                WeaknessEpisode.status == _OPEN_STATUS
            )
        )
    ).scalars().all()
    updated = 0
    for ep in rows:
        last_obs = ep.last_observed_at
        if last_obs.tzinfo is None:
            # SQLite drops tz on round-trip — assume UTC for math
            # so the subtraction below doesn't TypeError.
            last_obs = last_obs.replace(tzinfo=now.tzinfo)
        days = (now - last_obs).total_seconds() / _SECONDS_PER_DAY
        new_conf = decay_confidence(
            decay_per_day=ep.confidence_decay_per_day, days=days
        )
        if abs(new_conf - ep.confidence) > _TOLERANCE:
            ep.confidence = new_conf
            updated += 1
    if updated:
        await db.commit()
    return updated
