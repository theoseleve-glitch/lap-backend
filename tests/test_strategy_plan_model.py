"""T096 (sprint2-backend) — StrategyPlan ORM + migration.

Per-session pre-race intent: how many stints the driver planned,
how much fuel they planned to use, and which laps they planned
to pit on. T097 lands the StrategyEvent log (append-only timeline
of plan/armed/pit/stint_end/fcy/sc events); T098 lands the POST
endpoint that writes one of these rows.

Schema (per spec):
  - id (UUID7 PK)
  - session_id (FK → sessions, CASCADE delete; UNIQUE — one
    canonical pre-race plan per session; replans are tracked
    via T097's StrategyEvent log, not by mutating this row)
  - created_at (server-stamped; the "when did the driver lock
    in their plan" answer for the timeline)
  - planned_stints (int)
  - planned_fuel_liters (float)
  - planned_pit_laps (list[int] JSON; e.g., [25, 50])

Cascade delete from sessions: GDPR right-to-erasure on the
session purges the plan with it.

Two tests:
1. round-trip: insert → reload → all fields match (pin schema)
2. uniqueness: two StrategyPlans on the same session_id raise
   IntegrityError. Pin the constraint at the DB layer — ORM
   checks miss raw INSERTs and a duplicate plan would split
   the "what was the intent" answer in two.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, StrategyPlan, User
from infra.orm import Session as SessionRow

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def upgraded_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


async def _seed_session(factory: object) -> uuid6.UUID:
    """Build a minimal user/driver/session chain so the FK on
    strategy_plans.session_id has a target to point at."""
    async with factory() as db:  # type: ignore[operator]
        user = User(
            id=uuid6.uuid7(),
            clerk_user_id=f"t096-{uuid6.uuid7().hex}",
            email=f"t096-{uuid6.uuid7().hex}@t",
        )
        driver = DriverProfile(
            id=uuid6.uuid7(), user_id=user.id, display_name="D",
        )
        session = SessionRow(
            id=uuid6.uuid7(),
            driver_id=driver.id,
            manifest={},
            created_at=datetime(2026, 4, 27, tzinfo=UTC),
            status="processed",
        )
        db.add_all([user, driver, session])
        await db.commit()
        return session.id


# -- round-trip --------------------------------------------------


def test_strategy_plan_roundtrips_all_fields(
    upgraded_db_url: str,
) -> None:
    """Insert one row, reload, every field including the
    JSON pit-laps list comes back unchanged. Pins schema
    columns + types."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            session_id = await _seed_session(factory)
            row_id = uuid6.uuid7()
            now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
            async with factory() as db:
                row = StrategyPlan(
                    id=row_id,
                    session_id=session_id,
                    created_at=now,
                    planned_stints=3,
                    planned_fuel_liters=120.5,
                    planned_pit_laps=[25, 50],
                )
                db.add(row)
                await db.commit()
            async with factory() as db:
                loaded = (
                    await db.execute(select(StrategyPlan))
                ).scalar_one()
                assert loaded.id == row_id
                assert loaded.session_id == session_id
                assert loaded.planned_stints == 3
                assert loaded.planned_fuel_liters == 120.5
                assert loaded.planned_pit_laps == [25, 50]
        finally:
            await engine.dispose()

    asyncio.run(run())


# -- uniqueness --------------------------------------------------


def test_duplicate_session_id_raises(
    upgraded_db_url: str,
) -> None:
    """Two StrategyPlans with the same session_id must raise
    IntegrityError. Without the constraint, two pre-race plans
    would both be "the truth", and the timeline derivation in
    T097+ would have to pick one arbitrarily. Constraint must
    live at the DB layer — ORM checks miss raw INSERTs."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            session_id = await _seed_session(factory)
            now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
            async with factory() as db:
                db.add(
                    StrategyPlan(
                        id=uuid6.uuid7(),
                        session_id=session_id,
                        created_at=now,
                        planned_stints=2,
                        planned_fuel_liters=80.0,
                        planned_pit_laps=[40],
                    )
                )
                await db.commit()
            with pytest.raises(IntegrityError):
                async with factory() as db:
                    db.add(
                        StrategyPlan(
                            id=uuid6.uuid7(),
                            session_id=session_id,  # duplicate
                            created_at=now,
                            planned_stints=3,
                            planned_fuel_liters=90.0,
                            planned_pit_laps=[30, 60],
                        )
                    )
                    await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(run())
