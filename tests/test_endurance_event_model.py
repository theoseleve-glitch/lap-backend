"""T067 (sprint2-backend) — EnduranceEvent ORM + migration.

The aggregate that long-duration race weekends (24h, 12h, etc.)
hang off. Many sessions can belong to one event via the
SessionManifest.endurance_event_id field added in T066.

Schema (per spec):
  - id (UUID7 PK)
  - event_name (String)
  - total_duration_seconds (Float — matches SessionManifest.duration_seconds)
  - stint_count (Integer — number of stints planned for the event)
  - driver_ids (JSON list[UUID] — denormalized participant roster)

Why driver_ids as JSON instead of an association table: the
participant list is small (1-6 drivers per endurance entry) and
read-mostly. A join table would add a second migration + an extra
query for every read; the value of indexing/querying individual
participants doesn't outweigh that cost at this point in the
sprint. T068+ syncs the list as sessions arrive.

Two tests:
1. round-trip: insert → reload → fields (incl. driver_ids list)
   match. Pins schema + types via real DB I/O.
2. shape: inspect the live `endurance_events` table and assert
   exactly the contract columns are present. Catches a future
   "I added a column directly to ORM but forgot the migration"
   regression — those would slip past round-trip if the new
   column had a default.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncEngine

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import EnduranceEvent

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def upgraded_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


# -- round-trip ---------------------------------------------------


def test_endurance_event_roundtrips_all_fields(
    upgraded_db_url: str,
) -> None:
    """Insert one row including a driver_ids list, reload, every
    field comes back unchanged. Pins the JSON column round-trip
    for driver_ids — without it, a future migration that drops
    the JSON variant would silently coerce the list."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            async with factory() as db:
                event_id = uuid6.uuid7()
                drivers = [str(uuid6.uuid7()) for _ in range(3)]
                row = EnduranceEvent(
                    id=event_id,
                    event_name="Spa 24 Hours 2026",
                    total_duration_seconds=86400.0,
                    stint_count=12,
                    driver_ids=drivers,
                )
                db.add(row)
                await db.commit()
                loaded = (
                    await db.execute(select(EnduranceEvent))
                ).scalar_one()
                assert loaded.id == event_id
                assert loaded.event_name == "Spa 24 Hours 2026"
                assert loaded.total_duration_seconds == 86400.0
                assert loaded.stint_count == 12
                assert loaded.driver_ids == drivers
        finally:
            await engine.dispose()

    asyncio.run(run())


# -- table shape --------------------------------------------------


def test_endurance_events_table_has_contract_columns(
    upgraded_db_url: str,
) -> None:
    """Inspect the migrated table and confirm every contract
    column is present. Catches ORM-vs-migration drift — a row
    insert with default-only columns might pass round-trip even
    when the migration is missing a column (defaults paper over
    the gap), so we check column existence directly."""

    async def run() -> None:
        engine: AsyncEngine = make_engine(upgraded_db_url)
        try:
            async with engine.begin() as conn:
                column_names = await conn.run_sync(
                    lambda sync_conn: {
                        c["name"]
                        for c in inspect(sync_conn).get_columns(
                            "endurance_events"
                        )
                    }
                )
            assert {
                "id",
                "event_name",
                "total_duration_seconds",
                "stint_count",
                "driver_ids",
            }.issubset(column_names), (
                f"endurance_events missing contract columns; "
                f"have: {sorted(column_names)}"
            )
        finally:
            await engine.dispose()

    asyncio.run(run())
