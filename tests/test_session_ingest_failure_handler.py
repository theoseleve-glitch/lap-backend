"""T111 — final-failure handler marks Session "failed" with reason.

When `process_session_ingest` exhausts its retries (T110), Celery
fires the task's `on_failure` hook. The hook updates the Session row
to `status="failed"` and writes the exception text into a
`failure_reason` column so the UI / SSE stream can surface what
broke. Tests cover both layers: the async marker + the hook wiring.
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
from infra.celery_app import (
    SessionIngestTask,
    _mark_failed,
    process_session_ingest,
)
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
            db.add(User(id=user_id, clerk_user_id="u_t111", email="t111@t"))
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


def test_process_session_ingest_uses_session_ingest_task_class() -> None:
    # Wiring proof: the production task is an instance of the custom
    # base class whose `on_failure` performs the DB update.
    assert isinstance(process_session_ingest, SessionIngestTask)


def test_mark_failed_sets_status_and_reason(
    seeded_session: dict[str, Any],
) -> None:
    factory = seeded_session["factory"]
    session_id = seeded_session["session_id"]

    asyncio.run(_mark_failed(session_id, "boom: simulated failure"))

    async def _read() -> SessionRow:
        async with factory() as db:
            return (
                await db.execute(
                    select(SessionRow).where(SessionRow.id == session_id)
                )
            ).scalar_one()

    row = asyncio.run(_read())
    assert row.status == "failed"
    assert row.failure_reason == "boom: simulated failure"


@pytest.mark.usefixtures("seeded_session")
def test_mark_failed_raises_for_unknown_session() -> None:
    bogus = uuid6.uuid7()
    with pytest.raises(ValueError, match="session_not_found"):
        asyncio.run(_mark_failed(bogus, "any"))


def test_on_failure_hook_marks_session_failed(
    seeded_session: dict[str, Any],
) -> None:
    factory = seeded_session["factory"]
    session_id = seeded_session["session_id"]

    # Invoke the hook directly with the same shape Celery would use:
    # args contains the positional args passed to .delay(). Celery
    # calls the hook positionally, so we do the same.
    process_session_ingest.on_failure(
        RuntimeError("upstream timed out"),
        "task-xyz",
        (str(session_id),),
        {},
        None,
    )

    async def _read() -> SessionRow:
        async with factory() as db:
            return (
                await db.execute(
                    select(SessionRow).where(SessionRow.id == session_id)
                )
            ).scalar_one()

    row = asyncio.run(_read())
    assert row.status == "failed"
    assert row.failure_reason is not None
    assert "upstream timed out" in row.failure_reason


@pytest.mark.usefixtures("seeded_session")
def test_on_failure_hook_with_no_args_does_not_raise() -> None:
    # Defensive: if Celery somehow invokes on_failure without args
    # (unlikely but possible during scheduling edge cases), the hook
    # must not crash the worker.
    process_session_ingest.on_failure(
        RuntimeError("?"),
        "task-no-args",
        (),
        {},
        None,
    )
    # No assertion needed beyond "didn't raise".
