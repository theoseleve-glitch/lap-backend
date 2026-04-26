"""T085 (sprint2-backend) — iRacing calendar scraper.

Fetches the official-series + special-events calendar JSON from
iRacing's data API and upserts each entry into
iracing_calendar_entries (T085 ORM). Idempotent on re-run — the
weekly cron (T087) re-fires every Monday; INSERT-only would crash
on the second run, INSERT-IGNORE would persist forever-stale data
when iRacing renames an event.

Test-injectable transport (mirrors infra/clerk.py's pattern): the
test passes httpx.MockTransport with a canned response; production
passes None and httpx connects to the real API.

Out of scope for T085: parser refinement (T086) and cron wiring
(T087). The accepted JSON shape here is the minimal viable contract;
T086 will harden the parsing against real iRacing responses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import uuid6
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from infra.orm import IRacingCalendarEntry

CALENDAR_PATH = "/data/series/seasons"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


async def fetch_and_store_iracing_calendar(
    *,
    db: AsyncSession,
    base_url: str,
    transport: httpx.AsyncBaseTransport | None = None,
    now: datetime | None = None,
) -> int:
    """Fetch the calendar JSON, upsert into iracing_calendar_entries
    keyed on source_id, return count of rows touched.

    Match-then-update keeps the upsert dialect-portable (sqlite +
    postgres without dialect-specific INSERT...ON CONFLICT). At the
    iRacing schedule's volume (~hundreds of entries per fetch) the
    extra round-trips are cheap, and the cron is weekly — not a hot
    path.
    """
    fetched_at = now or datetime.now(UTC)
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.get(
            f"{base_url.rstrip('/')}{CALENDAR_PATH}"
        )
        resp.raise_for_status()
        payload: list[dict[str, Any]] = resp.json()

    touched = 0
    for entry in payload:
        existing = (
            await db.execute(
                select(IRacingCalendarEntry).where(
                    IRacingCalendarEntry.source_id == entry["source_id"]
                )
            )
        ).scalar_one_or_none()
        starts_at = _parse_dt(entry["starts_at"])
        assert starts_at is not None  # required field per contract
        ends_at = _parse_dt(entry.get("ends_at"))
        if existing is None:
            db.add(
                IRacingCalendarEntry(
                    id=uuid6.uuid7(),
                    source_id=entry["source_id"],
                    series_name=entry["series_name"],
                    event_name=entry["event_name"],
                    starts_at=starts_at,
                    ends_at=ends_at,
                    track_name=entry.get("track_name"),
                    car_class=entry.get("car_class"),
                    kind=entry["kind"],
                    fetched_at=fetched_at,
                )
            )
        else:
            existing.series_name = entry["series_name"]
            existing.event_name = entry["event_name"]
            existing.starts_at = starts_at
            existing.ends_at = ends_at
            existing.track_name = entry.get("track_name")
            existing.car_class = entry.get("car_class")
            existing.kind = entry["kind"]
            existing.fetched_at = fetched_at
        touched += 1

    await db.commit()
    return touched
