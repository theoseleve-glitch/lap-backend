"""T102 (sprint2-backend) — execution_score persistence column.

Adds `execution_score` to T096's StrategyPlan table — INTEGER
NULL. Null = "not computed yet" (the score lands when the
session's strategy data is finalized; intermediate plan-only
state has no actual yet, so no score). Per-session storage:
StrategyPlan is already UNIQUE on session_id, so attaching the
score there keeps it co-located with the plan it scores
against without a new table.

Two tests:
1. round-trip: insert a plan WITH execution_score → reload →
   the integer comes back unchanged. Pin: schema column +
   integer storage.
2. default-null: insert a plan WITHOUT execution_score (older
   code path, or pre-scoring state) → reload → execution_score
   is None. Pin: backwards-compatible default; existing rows
   from before T102 must keep working.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select

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
    async with factory() as db:  # type: ignore[operator]
        user = User(
            id=uuid6.uuid7(),
            clerk_user_id=f"t102-{uuid6.uuid7().hex}",
            email=f"t102-{uuid6.uuid7().hex}@t",
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


def test_execution_score_roundtrips(
    upgraded_db_url: str,
) -> None:
    """Insert a plan with execution_score=83, reload, the value
    comes back as 83. Pin: column exists, type is integer."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            session_id = await _seed_session(factory)
            async with factory() as db:
                db.add(
                    StrategyPlan(
                        id=uuid6.uuid7(),
                        session_id=session_id,
                        planned_stints=2,
                        planned_fuel_liters=80.0,
                        planned_pit_laps=[30],
                        execution_score=83,
                    )
                )
                await db.commit()
            async with factory() as db:
                loaded = (
                    await db.execute(select(StrategyPlan))
                ).scalar_one()
                assert loaded.execution_score == 83
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_execution_score_defaults_to_null_when_omitted(
    upgraded_db_url: str,
) -> None:
    """Insert WITHOUT execution_score (old POST endpoint path,
    or pre-scoring lifecycle state) → reload → None. Pin:
    backwards-compat with T098's POST handler that doesn't
    yet write the score, and with the "score not computed"
    state in general."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            session_id = await _seed_session(factory)
            async with factory() as db:
                db.add(
                    StrategyPlan(
                        id=uuid6.uuid7(),
                        session_id=session_id,
                        planned_stints=2,
                        planned_fuel_liters=80.0,
                        planned_pit_laps=[30],
                    )
                )
                await db.commit()
            async with factory() as db:
                loaded = (
                    await db.execute(select(StrategyPlan))
                ).scalar_one()
                assert loaded.execution_score is None
        finally:
            await engine.dispose()

    asyncio.run(run())
