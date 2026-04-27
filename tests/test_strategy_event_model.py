"""T097 (sprint2-backend) — StrategyEvent ORM + migration.

Append-only timeline of strategy-relevant events for a session.
Pairs with T096's StrategyPlan (the canonical pre-race intent):
this table is the runtime story — when the plan was set, when
the driver armed an alternate strategy, when each pit stop
happened, when stints ended, when an FCY (full course yellow)
or SC (safety car) period started.

Schema (per spec):
  - id (UUID7 PK)
  - session_id (FK → sessions, CASCADE delete)
  - kind (str ∈ {plan, armed, pit, stint_end, fcy, sc})
  - at_lap (int — the lap on which the event occurred)
  - details (JSON dict; kind-specific payload, e.g.,
    {"new_pit_lap": 32} for an "armed" event,
    {"laps_under_yellow": 3} for an "fcy" event)
  - created_at (server-stamped — for tie-breaking same-lap events)

NOT unique on (session_id, at_lap, kind) — multiple identical
events on the same lap are valid (e.g., two FCY periods both
starting at lap 5 in chaotic conditions). Append-only; the
order is preserved by (at_lap ASC, created_at ASC).

Cascade delete from sessions: GDPR right-to-erasure.

Two tests:
1. round-trip: insert → reload → all fields including JSON
   details come back unchanged. Pins schema columns + types.
2. multi-row: a session can carry many events, ordered correctly
   by (at_lap, created_at). Pin: append-only ordering matters
   for plan-vs-actual diff.
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
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, StrategyEvent, User
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
            clerk_user_id=f"t097-{uuid6.uuid7().hex}",
            email=f"t097-{uuid6.uuid7().hex}@t",
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


def test_strategy_event_roundtrips_all_fields(
    upgraded_db_url: str,
) -> None:
    """Insert one row, reload, every field including the JSON
    details payload comes back unchanged. Pins schema columns +
    types — a missing column or a wrong default would surface
    as either an IntegrityError on insert or a value mismatch
    on reload."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            session_id = await _seed_session(factory)
            row_id = uuid6.uuid7()
            now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
            async with factory() as db:
                row = StrategyEvent(
                    id=row_id,
                    session_id=session_id,
                    kind="armed",
                    at_lap=15,
                    details={"new_pit_lap": 32, "reason": "fcy"},
                    created_at=now,
                )
                db.add(row)
                await db.commit()
            async with factory() as db:
                loaded = (
                    await db.execute(select(StrategyEvent))
                ).scalar_one()
                assert loaded.id == row_id
                assert loaded.session_id == session_id
                assert loaded.kind == "armed"
                assert loaded.at_lap == 15
                assert loaded.details == {
                    "new_pit_lap": 32, "reason": "fcy",
                }
        finally:
            await engine.dispose()

    asyncio.run(run())


# -- multi-row ordering ------------------------------------------


def test_multiple_events_on_same_session_preserve_lap_order(
    upgraded_db_url: str,
) -> None:
    """Append three events out of lap order; reading sorted by
    (at_lap ASC, created_at ASC) yields the lap-ordered timeline.
    Pin: plan-vs-actual diff in T100+ relies on lap-order
    iteration; if the schema rejected one of the rows or the
    ordering broke, the diff would mis-compare."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            session_id = await _seed_session(factory)
            base = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
            async with factory() as db:
                # Inserted in non-lap order to prove the sort is by
                # at_lap, not insertion order.
                db.add_all([
                    StrategyEvent(
                        id=uuid6.uuid7(),
                        session_id=session_id,
                        kind="pit",
                        at_lap=30,
                        details={"stint_index": 1},
                        created_at=base + timedelta(seconds=2),
                    ),
                    StrategyEvent(
                        id=uuid6.uuid7(),
                        session_id=session_id,
                        kind="plan",
                        at_lap=0,
                        details={"planned_stints": 2},
                        created_at=base,
                    ),
                    StrategyEvent(
                        id=uuid6.uuid7(),
                        session_id=session_id,
                        kind="stint_end",
                        at_lap=30,
                        details={"stint_index": 1},
                        created_at=base + timedelta(seconds=3),
                    ),
                ])
                await db.commit()
            async with factory() as db:
                rows = (
                    await db.execute(
                        select(StrategyEvent)
                        .where(StrategyEvent.session_id == session_id)
                        .order_by(
                            StrategyEvent.at_lap.asc(),
                            StrategyEvent.created_at.asc(),
                        )
                    )
                ).scalars().all()
            assert [r.kind for r in rows] == [
                "plan", "pit", "stint_end",
            ]
            assert [r.at_lap for r in rows] == [0, 30, 30]
        finally:
            await engine.dispose()

    asyncio.run(run())
