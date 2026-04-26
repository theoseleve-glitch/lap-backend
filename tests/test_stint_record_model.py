"""T069 (sprint2-backend) — StintRecord ORM + migration.

Per-stint persistence for endurance sessions. One row per stint
of a session; T068 detects the stint boundaries, T070+ derive
the metrics that land on these rows.

Schema (per spec):
  - id (UUID7 PK)
  - session_id (FK → sessions, CASCADE delete)
  - stint_index (1-indexed sequence within the session)
  - start_lap / end_lap / lap_count (match T068's Stint dataclass)
  - tire_age_laps (laps already on tires at stint START; 0 = fresh)
  - fuel_used_liters (float)
  - lap_time_decay_seconds (float; T071 computes, T069 stores)

Uniqueness: (session_id, stint_index) — refuse double-writes for
the same stint of the same session, which would silently inflate
counts on the EnduranceEvent rollup.

Cascade delete from sessions: when a session is purged (e.g.,
GDPR right-to-erasure flow), its stints go too. The endurance
event itself stays (it can outlive any single contributor).

Two tests:
1. round-trip: insert → reload → all fields match. Pins schema.
2. uniqueness: two stints with the same (session_id, stint_index)
   raise IntegrityError. Pin the constraint at the DB layer
   (ORM-level checks would still let raw inserts slip through).
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
from infra.orm import DriverProfile, StintRecord, User
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
    stint_records.session_id has a target to point at."""
    async with factory() as db:  # type: ignore[operator]
        user = User(id=uuid6.uuid7(), clerk_user_id="t069", email="t069@t")
        driver = DriverProfile(
            id=uuid6.uuid7(), user_id=user.id, display_name="D",
        )
        session = SessionRow(
            id=uuid6.uuid7(),
            driver_id=driver.id,
            manifest={},
            created_at=datetime(2026, 4, 26, tzinfo=UTC),
            status="processed",
        )
        db.add_all([user, driver, session])
        await db.commit()
        return session.id


# -- round-trip ---------------------------------------------------


def test_stint_record_roundtrips_all_fields(
    upgraded_db_url: str,
) -> None:
    """Insert one row, reload, every field including the metric
    floats comes back unchanged. Pins schema columns + types."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            session_id = await _seed_session(factory)
            row_id = uuid6.uuid7()
            async with factory() as db:
                row = StintRecord(
                    id=row_id,
                    session_id=session_id,
                    stint_index=2,
                    start_lap=13,
                    end_lap=27,
                    lap_count=15,
                    tire_age_laps=12,
                    fuel_used_liters=43.7,
                    lap_time_decay_seconds=0.082,
                )
                db.add(row)
                await db.commit()
            async with factory() as db:
                loaded = (
                    await db.execute(select(StintRecord))
                ).scalar_one()
                assert loaded.id == row_id
                assert loaded.session_id == session_id
                assert loaded.stint_index == 2
                assert loaded.start_lap == 13
                assert loaded.end_lap == 27
                assert loaded.lap_count == 15
                assert loaded.tire_age_laps == 12
                assert loaded.fuel_used_liters == 43.7
                assert loaded.lap_time_decay_seconds == 0.082
        finally:
            await engine.dispose()

    asyncio.run(run())


# -- uniqueness ---------------------------------------------------


def test_duplicate_session_stint_index_pair_raises(
    upgraded_db_url: str,
) -> None:
    """Two StintRecords with the same (session_id, stint_index)
    must raise IntegrityError. Without the constraint a re-fire
    of the stint-detection job would silently double-count and
    skew per-session pace metrics. The constraint must live at
    the DB layer — ORM-level checks miss raw INSERTs."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            session_id = await _seed_session(factory)
            async with factory() as db:
                db.add(
                    StintRecord(
                        id=uuid6.uuid7(),
                        session_id=session_id,
                        stint_index=1,
                        start_lap=1,
                        end_lap=10,
                        lap_count=10,
                        tire_age_laps=0,
                        fuel_used_liters=30.0,
                        lap_time_decay_seconds=0.05,
                    )
                )
                await db.commit()
            with pytest.raises(IntegrityError):
                async with factory() as db:
                    db.add(
                        StintRecord(
                            id=uuid6.uuid7(),
                            session_id=session_id,
                            stint_index=1,  # duplicate
                            start_lap=1,
                            end_lap=10,
                            lap_count=10,
                            tire_age_laps=0,
                            fuel_used_liters=30.0,
                            lap_time_decay_seconds=0.05,
                        )
                    )
                    await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(run())
