"""T122 — persist `SessionRecord` row after processing.

The Celery `process_session_ingest` task creates one
`SessionRecord` row per session at the end of processing. The row's
payload (rich per ADR-006) starts empty here; the integration tests
in T124/T125 will pin a fuller shape once the full pipeline runs.

Persistence is upsert-style so a Celery retry that re-enters the
task body after a downstream failure doesn't trip the unique
session_id constraint and mislead operators reading logs.
"""

import asyncio
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select

from alembic import command
from infra.celery_app import _persist_session_record, process_session_ingest
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, SessionRecord, User
from infra.orm import Session as SessionRow

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def seeded_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[dict[str, Any], None, None]:
    db_path = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)

    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    session_id = uuid6.uuid7()

    async def _seed() -> None:
        async with factory() as db:
            db.add(User(id=user_id, clerk_user_id="u_t122", email="t122@t"))
            db.add(
                DriverProfile(
                    id=driver_id, user_id=user_id, display_name="A"
                )
            )
            db.add(
                SessionRow(
                    id=session_id,
                    driver_id=driver_id,
                    manifest={"bundle_version": "v2"},
                    status="processing",
                )
            )
            await db.commit()

    asyncio.run(_seed())
    yield {"factory": factory, "session_id": session_id}


def test_persist_session_record_inserts_row(
    seeded_session: dict[str, Any],
) -> None:
    factory = seeded_session["factory"]
    session_id = seeded_session["session_id"]

    asyncio.run(_persist_session_record(session_id, payload={"k": "v"}))

    async def _read() -> SessionRecord:
        async with factory() as db:
            return (
                await db.execute(
                    select(SessionRecord).where(
                        SessionRecord.session_id == session_id
                    )
                )
            ).scalar_one()

    row = asyncio.run(_read())
    assert row.session_id == session_id
    assert row.payload == {"k": "v"}


def test_persist_session_record_overwrites_existing_payload(
    seeded_session: dict[str, Any],
) -> None:
    # Idempotency check: a Celery retry re-enters the task body. The
    # second persist must overwrite the first, not create a duplicate
    # row (which would violate the unique session_id constraint).
    factory = seeded_session["factory"]
    session_id = seeded_session["session_id"]

    asyncio.run(_persist_session_record(session_id, payload={"v": 1}))
    asyncio.run(_persist_session_record(session_id, payload={"v": 2}))

    async def _read_all() -> list[SessionRecord]:
        async with factory() as db:
            return list(
                (
                    await db.execute(
                        select(SessionRecord).where(
                            SessionRecord.session_id == session_id
                        )
                    )
                ).scalars()
            )

    rows = asyncio.run(_read_all())
    assert len(rows) == 1
    assert rows[0].payload == {"v": 2}


def test_process_session_ingest_creates_session_record_row(
    seeded_session: dict[str, Any],
) -> None:
    # End-to-end check on the wiring: running the task body produces
    # a SessionRecord row for the session.
    factory = seeded_session["factory"]
    session_id = seeded_session["session_id"]

    process_session_ingest.run(str(session_id))

    async def _read_count() -> int:
        async with factory() as db:
            return len(
                list(
                    (
                        await db.execute(
                            select(SessionRecord).where(
                                SessionRecord.session_id == session_id
                            )
                        )
                    ).scalars()
                )
            )

    assert asyncio.run(_read_count()) == 1
