"""T089 (sprint2-backend) — driver readiness recompute domain.

When a TargetEvent is pinned (T082) or unpinned (T083), the
driver's DriverReadinessState must be refreshed so downstream
consumers (T090 plan generation, T093 countdown endpoint) see
the new target context.

Recompute contract:
  - Reads target_events scoped to driver_id
  - Picks the SOONEST UPCOMING pin (starts_at >= now, ASC) as
    the target_event JSON. Past events don't shape near-term
    readiness — the user is preparing for the next thing.
  - When no upcoming pin exists: target_event=None (the user
    has no near-term goal).
  - Appends a new DriverReadinessState row (history-style;
    DriverReadinessState has no UNIQUE on driver_id — each
    recompute is a snapshot in time).
  - Returns the inserted row.

This file tests the pure-domain function. The pin/unpin
endpoint hook lives in app/routers/target_events.py and is
covered separately by test_readiness_recompute_on_pin.py.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select

from alembic import command
from domain.readiness_recompute import recompute_driver_readiness
from infra.db import make_engine, make_session_factory
from infra.orm import (
    DriverProfile,
    DriverReadinessState,
    TargetEvent,
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


async def _seed_driver(db: object) -> uuid6.UUID:
    user = User(
        id=uuid6.uuid7(),
        clerk_user_id=f"u-{uuid6.uuid7().hex}",
        email=f"{uuid6.uuid7().hex}@t",
    )
    driver = DriverProfile(
        id=uuid6.uuid7(), user_id=user.id, display_name="t089",
    )
    db.add_all([user, driver])
    await db.commit()
    return driver.id


def _add_pin(
    db: object,
    *,
    driver_id: uuid6.UUID,
    name: str,
    starts_at: datetime,
    competitive_weight: float = 0.5,
) -> uuid6.UUID:
    """Build (don't commit) a TargetEvent row attached to driver."""
    event_id = uuid6.uuid7()
    db.add(
        TargetEvent(
            id=event_id,
            driver_id=driver_id,
            name=name,
            kind="sim_event",
            starts_at=starts_at,
            ends_at=None,
            track_id=None,
            car_id=None,
            competitive_weight=competitive_weight,
            notes=None,
        )
    )
    return event_id


# -- no pins ------------------------------------------------------


def test_recompute_with_no_pins_writes_state_with_null_target(
    upgraded_db_url: str,
) -> None:
    """Driver with zero pins → recompute writes a snapshot with
    target_event=None. Pin: must NOT skip the write — downstream
    consumers (T090) need the snapshot to know the driver has
    no current goal, distinct from "no readiness ever computed."
    """

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            async with factory() as db:
                driver_id = await _seed_driver(db)
                now = datetime(2026, 4, 27, 10, 0, tzinfo=UTC)
                state = await recompute_driver_readiness(
                    db, driver_id=driver_id, now=now,
                )
                assert state.target_event is None
                rows = (
                    await db.execute(
                        select(DriverReadinessState)
                        .where(DriverReadinessState.driver_id == driver_id)
                    )
                ).scalars().all()
                assert len(rows) == 1
                assert rows[0].target_event is None
        finally:
            await engine.dispose()

    asyncio.run(run())


# -- soonest upcoming wins ----------------------------------------


def test_recompute_picks_soonest_upcoming_pin_as_target(
    upgraded_db_url: str,
) -> None:
    """Two upcoming pins — one in 2 weeks, one in 6 months.
    Recompute must pick the 2-week event as the target. The user
    is preparing for the next thing; later pins are noise for
    near-term readiness. Pin: a regression to "highest
    competitive_weight wins" or "most-recently-pinned wins"
    would surface the wrong event."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            now = datetime(2026, 4, 27, 10, 0, tzinfo=UTC)
            soon = now + timedelta(days=14)
            far = now + timedelta(days=180)
            async with factory() as db:
                driver_id = await _seed_driver(db)
                _add_pin(
                    db, driver_id=driver_id, name="far",
                    starts_at=far, competitive_weight=0.95,
                )
                soon_id = _add_pin(
                    db, driver_id=driver_id, name="soon",
                    starts_at=soon, competitive_weight=0.4,
                )
                await db.commit()
                state = await recompute_driver_readiness(
                    db, driver_id=driver_id, now=now,
                )
                assert state.target_event is not None
                assert state.target_event["id"] == str(soon_id)
                assert state.target_event["name"] == "soon"
        finally:
            await engine.dispose()

    asyncio.run(run())


# -- past events ignored -----------------------------------------


def test_recompute_skips_past_events_when_picking_target(
    upgraded_db_url: str,
) -> None:
    """A past pin (starts_at < now) is in the rear view; it must
    not become the readiness target. With only past pins and no
    upcoming, target_event=None — the driver has no near-term
    goal. Pin: missing this filter would cause the readiness UI
    to show last month's race as "your next event"."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            now = datetime(2026, 4, 27, 10, 0, tzinfo=UTC)
            past = now - timedelta(days=30)
            async with factory() as db:
                driver_id = await _seed_driver(db)
                _add_pin(
                    db, driver_id=driver_id, name="past",
                    starts_at=past,
                )
                await db.commit()
                state = await recompute_driver_readiness(
                    db, driver_id=driver_id, now=now,
                )
                assert state.target_event is None
        finally:
            await engine.dispose()

    asyncio.run(run())


# -- competitive_weight feeds event_pressure_input ---------------


def test_recompute_uses_competitive_weight_as_event_pressure(
    upgraded_db_url: str,
) -> None:
    """ADR §5.7: event_pressure_input is driven by the target
    event's competitive_weight. Pin the wiring — without it,
    target-aware plan generation in T090 has no signal to
    weight prescriptions toward the upcoming event."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            now = datetime(2026, 4, 27, 10, 0, tzinfo=UTC)
            async with factory() as db:
                driver_id = await _seed_driver(db)
                _add_pin(
                    db, driver_id=driver_id, name="A-list race",
                    starts_at=now + timedelta(days=7),
                    competitive_weight=0.92,
                )
                await db.commit()
                state = await recompute_driver_readiness(
                    db, driver_id=driver_id, now=now,
                )
                assert state.event_pressure_input == 0.92, (
                    f"event_pressure_input must mirror the pinned "
                    f"event's competitive_weight; got "
                    f"{state.event_pressure_input}"
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


# -- ownership scoping pin ---------------------------------------


def test_recompute_ignores_other_drivers_pins(
    upgraded_db_url: str,
) -> None:
    """Two drivers; A has no pins, B has an upcoming pin. A's
    recompute must NOT pick up B's pin as its target. Pin: the
    query must filter by driver_id; without it, every driver
    would inherit the next-soonest event from anyone."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            now = datetime(2026, 4, 27, 10, 0, tzinfo=UTC)
            async with factory() as db:
                driver_a = await _seed_driver(db)
                driver_b = await _seed_driver(db)
                _add_pin(
                    db, driver_id=driver_b, name="B-only pin",
                    starts_at=now + timedelta(days=7),
                )
                await db.commit()
                state_a = await recompute_driver_readiness(
                    db, driver_id=driver_a, now=now,
                )
                assert state_a.target_event is None, (
                    "driver A must not see driver B's pin as "
                    "their readiness target"
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


# -- history-style: each recompute appends ----------------------


def test_recompute_appends_snapshot_does_not_overwrite(
    upgraded_db_url: str,
) -> None:
    """DriverReadinessState is a history table (no UNIQUE on
    driver_id). Each call must append a row, not update in
    place. Pin: T093 countdown endpoint and the admin trends
    view will rely on the snapshot trail to show readiness
    drift over time."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            now = datetime(2026, 4, 27, 10, 0, tzinfo=UTC)
            async with factory() as db:
                driver_id = await _seed_driver(db)
                await recompute_driver_readiness(
                    db, driver_id=driver_id, now=now,
                )
                await recompute_driver_readiness(
                    db,
                    driver_id=driver_id,
                    now=now + timedelta(hours=1),
                )
                rows = (
                    await db.execute(
                        select(DriverReadinessState).where(
                            DriverReadinessState.driver_id == driver_id
                        )
                    )
                ).scalars().all()
                assert len(rows) == 2
        finally:
            await engine.dispose()

    asyncio.run(run())
