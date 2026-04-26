"""T081 (sprint2-backend) — TargetEvent ORM + migration.

The pinned upcoming sim event or real-life track day that the
driver is preparing for. Field shapes follow ADR §5.7 verbatim:
  - id (UUID7 PK)
  - driver_id (FK driver_profiles, CASCADE — GDPR right-to-erasure
    drops the pinned targets when the driver is purged)
  - name (String — e.g., "iRacing 24h Daytona qualifier")
  - kind (String — Literal["sim_event", "real_track_day",
    "real_race", "real_test"]; enforced at the Pydantic boundary
    in T082+ when the request schema lands)
  - starts_at (DateTime tz-aware — RFC3339 UTC per CLAUDE.md)
  - ends_at (DateTime tz-aware, nullable — multi-day events;
    None for point-in-time events)
  - track_id (FK track_layouts, nullable — real-life events may
    reference a track not yet in the catalog)
  - car_id (FK car_models, nullable — same rationale)
  - competitive_weight (Float in [0, 1] — drives the
    event_pressure_input in DriverReadinessState per ADR §5.7)
  - notes (Text, nullable — free-form context the driver pins)

Why driver_id added beyond ADR §5.7's listed fields: each pinned
target belongs to one driver. The ADR §5.7 BaseModel is the
event shape itself; this is the persistence layer where the
driver→event relationship lives. T082 (POST) will derive
driver_id from the authenticated user — the column has to exist
first.

Three tests:
  1. round-trip: insert with all fields populated → reload →
     every field comes back unchanged. Pins schema + types via
     real DB I/O. Seeds the FK parents (user, driver, track, car).
  2. nullable optionals omitted: insert with only the required
     fields → optional FK + ends_at + notes default to None. Pins
     the ADR §5.7 Optional[] contract — without this, a future
     migration that flips nullable=False would silently break the
     POST endpoint for point-in-time events.
  3. table shape: introspect target_events and assert every
     contract column is present. Catches ORM-vs-migration drift —
     a row insert with default-only columns might pass round-trip
     even when the migration is missing a column (defaults paper
     over the gap), so we check column existence directly.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncEngine

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import (
    CarModel,
    DriverProfile,
    TargetEvent,
    TrackLayout,
    User,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def upgraded_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


def _seed_driver_track_car(
    db: object,
) -> tuple[uuid6.UUID, uuid6.UUID, uuid6.UUID]:
    """Insert one user+driver+track+car so a TargetEvent FK chain
    has parents to point at. Returns (driver_id, track_id, car_id)."""
    user = User(
        id=uuid6.uuid7(),
        clerk_user_id="clerk_t081",
        email="t081@test",
    )
    driver = DriverProfile(
        id=uuid6.uuid7(),
        user_id=user.id,
        display_name="t081-driver",
    )
    track = TrackLayout(
        id=uuid6.uuid7(),
        source_provider="iracing",
        name="Daytona International Speedway",
        layout_name="24h",
        start_finish_lat=29.185,
        start_finish_lon=-81.071,
        start_finish_heading_deg=270.0,
        sector_checkpoints=[],
        corner_names=[],
        master_track_id=uuid6.uuid7(),
    )
    car = CarModel(
        id=uuid6.uuid7(),
        source_provider="iracing",
        name="Ferrari 296 GT3",
        car_class="gt3",
        master_car_id=uuid6.uuid7(),
    )
    db.add_all([user, driver, track, car])
    return driver.id, track.id, car.id


# -- round-trip ---------------------------------------------------


def test_target_event_roundtrips_all_fields(
    upgraded_db_url: str,
) -> None:
    """Insert one TargetEvent with every field populated, reload,
    every field comes back unchanged. Pins schema + types via real
    DB I/O — including DateTime tz-aware round-trip and the FK
    chain to driver/track/car."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            async with factory() as db:
                driver_id, track_id, car_id = _seed_driver_track_car(db)
                await db.flush()
                event_id = uuid6.uuid7()
                starts = datetime(2026, 12, 12, 18, 0, tzinfo=UTC)
                ends = datetime(2026, 12, 13, 18, 0, tzinfo=UTC)
                row = TargetEvent(
                    id=event_id,
                    driver_id=driver_id,
                    name="iRacing 24h Daytona Qualifier",
                    kind="sim_event",
                    starts_at=starts,
                    ends_at=ends,
                    track_id=track_id,
                    car_id=car_id,
                    competitive_weight=0.85,
                    notes="Need to nail the bus stop chicane",
                )
                db.add(row)
                await db.commit()
                loaded = (
                    await db.execute(select(TargetEvent))
                ).scalar_one()
                assert loaded.id == event_id
                assert loaded.driver_id == driver_id
                assert loaded.name == "iRacing 24h Daytona Qualifier"
                assert loaded.kind == "sim_event"
                assert loaded.starts_at == starts
                assert loaded.ends_at == ends
                assert loaded.track_id == track_id
                assert loaded.car_id == car_id
                assert loaded.competitive_weight == 0.85
                assert loaded.notes == "Need to nail the bus stop chicane"
        finally:
            await engine.dispose()

    asyncio.run(run())


# -- optional fields default to None ------------------------------


def test_target_event_optional_fields_default_to_none(
    upgraded_db_url: str,
) -> None:
    """Insert with only the ADR-required fields populated: track_id,
    car_id, ends_at, and notes must accept None. Pins ADR §5.7's
    Optional[] contract — a future nullable=False would block the
    POST endpoint for point-in-time real-life events whose track
    isn't in the catalog yet."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            async with factory() as db:
                driver_id, _track_id, _car_id = _seed_driver_track_car(db)
                await db.flush()
                event_id = uuid6.uuid7()
                starts = datetime(2026, 6, 14, 9, 0, tzinfo=UTC)
                row = TargetEvent(
                    id=event_id,
                    driver_id=driver_id,
                    name="Local karting test",
                    kind="real_test",
                    starts_at=starts,
                    competitive_weight=0.2,
                )
                db.add(row)
                await db.commit()
                loaded = (
                    await db.execute(select(TargetEvent))
                ).scalar_one()
                assert loaded.ends_at is None
                assert loaded.track_id is None
                assert loaded.car_id is None
                assert loaded.notes is None
        finally:
            await engine.dispose()

    asyncio.run(run())


# -- table shape --------------------------------------------------


def test_target_events_table_has_contract_columns(
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
                            "target_events"
                        )
                    }
                )
            assert {
                "id",
                "driver_id",
                "name",
                "kind",
                "starts_at",
                "ends_at",
                "track_id",
                "car_id",
                "competitive_weight",
                "notes",
            }.issubset(column_names), (
                f"target_events missing contract columns; "
                f"have: {sorted(column_names)}"
            )
        finally:
            await engine.dispose()

    asyncio.run(run())
