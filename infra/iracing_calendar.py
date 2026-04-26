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


def _track_display_name(track: dict[str, Any]) -> str:
    """Compose the display name from iRacing's track + config.

    Spa GP and Spa endurance share track_name "Spa-Francorchamps"
    but differ on config_name. Concatenating distinguishes them in
    the entry's free-text track_name; downstream T086+ catalog
    bridge can still resolve to a single TrackLayout.
    """
    name = track.get("track_name") or ""
    config = track.get("config_name")
    if config:
        return f"{name} {config}".strip()
    return name


def parse_iracing_calendar_payload(
    raw: dict[str, Any],
) -> list[dict[str, Any]]:
    """T086 — convert raw iRacing API response to entry dicts.

    Input shape (representative — actual iRacing schema is similar
    but verbose):
      {"series": [{series_id, series_name, car_classes, schedules: [
          {race_week_num, track, start_date, end_date}, ...]}],
       "special_events": [{event_id, event_name, track,
          start_at, end_at}]}

    Output is the entry-shape list T085's
    fetch_and_store_iracing_calendar consumes. source_id is the
    upsert key — it MUST be deterministic across re-parses (T085's
    cron re-fires weekly; nondeterminism would duplicate every row).

    Defensive against missing top-level arrays — iRacing may omit
    `special_events` off-season or `series` between seasons; the
    cron must not crash.
    """
    entries: list[dict[str, Any]] = []
    for series in raw.get("series", []) or []:
        series_id = series["series_id"]
        car_classes = series.get("car_classes") or []
        # One series ⇒ one car class in iRacing's data shape; if a
        # series has multiple, take the first (the parser stays
        # representative; T086 doesn't try to model multi-class
        # endurance series, which use the special_events path).
        car_class = car_classes[0]["name"] if car_classes else None
        for schedule in series.get("schedules", []) or []:
            week = schedule["race_week_num"]
            entries.append({
                "source_id": f"ir-series-{series_id}-week-{week}",
                "series_name": series["series_name"],
                "event_name": (
                    f"{series['series_name']} — Week {week}"
                ),
                "starts_at": schedule["start_date"],
                "ends_at": schedule.get("end_date"),
                "track_name": _track_display_name(schedule["track"]),
                "car_class": car_class,
                "kind": "series_week",
            })
    for special in raw.get("special_events", []) or []:
        entries.append({
            "source_id": f"ir-special-{special['event_id']}",
            "series_name": "iRacing Special Events",
            "event_name": special["event_name"],
            "starts_at": special["start_at"],
            "ends_at": special.get("end_at"),
            "track_name": _track_display_name(special["track"]),
            # Specials run multi-class — leave None rather than
            # picking one arbitrarily; T092's filter treats None
            # as "open to any car class".
            "car_class": None,
            "kind": "special_event",
        })
    return entries


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
