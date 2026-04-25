"""T123 — `process_session_ingest` flips Session.status to "ready".

After all processing steps complete, the row transitions from
"processing" to "ready". The router exposed by GET /sessions/:id
reads this column directly so clients (and the SSE consumer) can
tell the work is finished.
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
from infra.celery_app import _mark_ready, process_session_ingest
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, User
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
            db.add(User(id=user_id, clerk_user_id="u_t123", email="t123@t"))
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


def test_mark_ready_sets_status_to_ready(
    seeded_session: dict[str, Any],
) -> None:
    factory = seeded_session["factory"]
    session_id = seeded_session["session_id"]

    asyncio.run(_mark_ready(session_id))

    async def _read() -> SessionRow:
        async with factory() as db:
            return (
                await db.execute(
                    select(SessionRow).where(SessionRow.id == session_id)
                )
            ).scalar_one()

    row = asyncio.run(_read())
    assert row.status == "ready"


@pytest.mark.usefixtures("seeded_session")
def test_mark_ready_raises_for_unknown_session() -> None:
    bogus = uuid6.uuid7()
    with pytest.raises(ValueError, match="session_not_found"):
        asyncio.run(_mark_ready(bogus))


def test_process_session_ingest_marks_ready_after_persist(
    seeded_session: dict[str, Any],
) -> None:
    # End-to-end on the wiring: running the task body transitions the
    # session through processing → ready.
    factory = seeded_session["factory"]
    session_id = seeded_session["session_id"]

    process_session_ingest.run(str(session_id))

    async def _read_status() -> str:
        async with factory() as db:
            row = (
                await db.execute(
                    select(SessionRow).where(SessionRow.id == session_id)
                )
            ).scalar_one()
            return str(row.status)

    assert asyncio.run(_read_status()) == "ready"
