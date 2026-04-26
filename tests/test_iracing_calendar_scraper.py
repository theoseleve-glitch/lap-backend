"""T085 (sprint2-backend) — iRacing calendar scraper.

Weekly fetch of official series + special events. The scraper
takes an injectable httpx transport (so the test can hand it a
canned response without making real network calls) and persists
the parsed entries into iracing_calendar_entries (T085 migration).

Re-fetch semantics matter: this is a weekly cron (T087). Plain
INSERT would crash on the second run; INSERT-IGNORE would persist
forever-stale data when iRacing renames an event. The contract is
upsert keyed on source_id — re-fetching with a changed
event_name overwrites the row in place.

Three tests:
  1. first fetch persists every entry with every field
  2. re-fetch with a CHANGED event_name upserts (no duplicate row,
     and the changed value is reflected). This single test proves
     two properties at once: no-dup AND not-stale.
  3. table-shape introspection — catches ORM-vs-migration drift
     where the ORM has a column the migration omitted.

Out of scope for T085: the parser (T086) and the cron (T087). The
in-scope JSON shape is the minimal viable: a top-level array of
entry objects with stable keys. T086 will refine this against
real iRacing API responses.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncEngine

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.iracing_calendar import fetch_and_store_iracing_calendar
from infra.orm import IRacingCalendarEntry

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def upgraded_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


def _payload_v1() -> list[dict[str, Any]]:
    """Two-entry calendar payload covering both kinds."""
    return [
        {
            "source_id": "ir-2026-s2-w05-porsche",
            "series_name": "Porsche Cup 2026 Season 2",
            "event_name": "Week 5 — Spa Francorchamps",
            "starts_at": "2026-05-04T00:00:00+00:00",
            "ends_at": "2026-05-10T23:59:59+00:00",
            "track_name": "Spa-Francorchamps GP",
            "car_class": "Porsche 911 Cup",
            "kind": "series_week",
        },
        {
            "source_id": "ir-2026-special-daytona24",
            "series_name": "iRacing Special Events",
            "event_name": "Daytona 24 Hours 2026",
            "starts_at": "2026-12-12T18:00:00+00:00",
            "ends_at": "2026-12-13T18:00:00+00:00",
            "track_name": "Daytona International Speedway 24h",
            "car_class": None,
            "kind": "special_event",
        },
    ]


def _make_transport(
    payload: list[dict[str, Any]],
) -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)
    return httpx.MockTransport(handler)


def _tzaware(dt: datetime | None) -> datetime | None:
    """SQLite's DateTime(timezone=True) loses tzinfo on cross-
    object reload (the session GCs the inserted row's strong ref
    once the scraper returns). Re-stamp UTC for the test
    comparison — Postgres preserves tz, so production isn't
    affected. The actual y/m/d/h/m values are what we're pinning."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# -- first-fetch round-trip ---------------------------------------


def test_first_fetch_persists_entries_with_all_fields(
    upgraded_db_url: str,
) -> None:
    """Empty DB, two-entry mock payload, call scraper. Every field
    on every entry round-trips through persistence — including the
    two distinct kinds and the nullable car_class on the special
    event. fetched_at is non-null on every row (server-stamped, not
    payload-supplied)."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            transport = _make_transport(_payload_v1())
            now = datetime(2026, 4, 27, 8, 0, tzinfo=UTC)
            # Single-session read avoids SQLite's tz-strip on
            # cross-session reload (matches T081 pattern). Postgres
            # preserves tz, so production behavior is unaffected.
            async with factory() as db:
                touched = await fetch_and_store_iracing_calendar(
                    db=db,
                    base_url="https://members-ng.iracing.test",
                    transport=transport,
                    now=now,
                )
                assert touched == 2
                rows = (
                    await db.execute(
                        select(IRacingCalendarEntry)
                        .order_by(IRacingCalendarEntry.source_id)
                    )
                ).scalars().all()
                assert len(rows) == 2
                porsche, daytona = rows
                assert porsche.source_id == "ir-2026-s2-w05-porsche"
                assert porsche.series_name == "Porsche Cup 2026 Season 2"
                assert porsche.event_name == "Week 5 — Spa Francorchamps"
                assert _tzaware(porsche.starts_at) == datetime(
                    2026, 5, 4, 0, 0, tzinfo=UTC
                )
                assert _tzaware(porsche.ends_at) == datetime(
                    2026, 5, 10, 23, 59, 59, tzinfo=UTC
                )
                assert porsche.track_name == "Spa-Francorchamps GP"
                assert porsche.car_class == "Porsche 911 Cup"
                assert porsche.kind == "series_week"
                assert _tzaware(porsche.fetched_at) == now
                # Special event with null car_class
                assert daytona.kind == "special_event"
                assert daytona.car_class is None
                assert _tzaware(daytona.fetched_at) == now
        finally:
            await engine.dispose()

    asyncio.run(run())


# -- re-fetch upsert ----------------------------------------------


def test_refetch_with_changed_entry_upserts_not_duplicates(
    upgraded_db_url: str,
) -> None:
    """Fetch the v1 payload, then fetch a v2 payload where the
    Porsche entry's event_name has been corrected. Pin BOTH:
      - row count stays at 2 (no-duplicate / idempotent)
      - the Porsche row's event_name reflects the v2 string
        (upsert / not stale)

    A regression to INSERT-only would crash on second fetch; a
    regression to INSERT-IGNORE would keep the v1 stale name
    forever. Both modes break this test.
    """

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            payload_v1 = _payload_v1()
            payload_v2 = _payload_v1()
            payload_v2[0]["event_name"] = (
                "Week 5 — Spa Francorchamps (revised)"
            )
            now1 = datetime(2026, 4, 27, 8, 0, tzinfo=UTC)
            now2 = datetime(2026, 5, 4, 8, 0, tzinfo=UTC)
            # First fetch (v1) in its own session, then reuse the
            # second session for the v2 fetch + verification — this
            # both exercises the cross-session existing-row path
            # (the upsert lookup must still find row inserted in a
            # prior session) AND keeps the post-v2 reads on the same
            # session that issued the v2 update (avoids SQLite's
            # tz-strip on cross-session datetime reads).
            async with factory() as db:
                await fetch_and_store_iracing_calendar(
                    db=db,
                    base_url="https://members-ng.iracing.test",
                    transport=_make_transport(payload_v1),
                    now=now1,
                )
            async with factory() as db:
                touched = await fetch_and_store_iracing_calendar(
                    db=db,
                    base_url="https://members-ng.iracing.test",
                    transport=_make_transport(payload_v2),
                    now=now2,
                )
                assert touched == 2
                rows = (
                    await db.execute(
                        select(IRacingCalendarEntry).order_by(
                            IRacingCalendarEntry.source_id
                        )
                    )
                ).scalars().all()
                assert len(rows) == 2, (
                    f"expected upsert (2 rows) but got {len(rows)} — "
                    f"a regression to plain INSERT would crash, but "
                    f"an INSERT IGNORE would leave 2 stale rows from v1"
                )
                porsche = rows[0]
                assert porsche.event_name == (
                    "Week 5 — Spa Francorchamps (revised)"
                ), (
                    "v1's event_name persisted across the re-fetch — "
                    "upsert is broken; the second call did not update "
                    "the existing row"
                )
                assert _tzaware(porsche.fetched_at) == now2
        finally:
            await engine.dispose()

    asyncio.run(run())


# -- table shape --------------------------------------------------


def test_iracing_calendar_entries_table_has_contract_columns(
    upgraded_db_url: str,
) -> None:
    """Inspect migrated table; every contract column present.
    Catches ORM-vs-migration drift — the round-trip test could
    pass if the migration is missing a nullable column (defaults
    paper over the gap), so check the column shape directly."""

    async def run() -> None:
        engine: AsyncEngine = make_engine(upgraded_db_url)
        try:
            async with engine.begin() as conn:
                column_names = await conn.run_sync(
                    lambda sync_conn: {
                        c["name"]
                        for c in inspect(sync_conn).get_columns(
                            "iracing_calendar_entries"
                        )
                    }
                )
            assert {
                "id",
                "source_id",
                "series_name",
                "event_name",
                "starts_at",
                "ends_at",
                "track_name",
                "car_class",
                "kind",
                "fetched_at",
            }.issubset(column_names), (
                f"iracing_calendar_entries missing contract columns; "
                f"have: {sorted(column_names)}"
            )
        finally:
            await engine.dispose()

    asyncio.run(run())
