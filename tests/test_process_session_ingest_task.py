"""T109 — process_session_ingest skeleton transitions session status.

The task is dispatched by the ingest endpoint (T103) and runs in a
Celery worker. Its v1 job is small but real: load the manifest from
the Session row, transition status to "processing", and log.
Subsequent tasks (T110+) will add retry semantics and the actual
processing pipeline.

Tests run the task under eager mode with a fresh sqlite DB so the
side effect (status transition) is observable without spinning up
an actual worker.
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
from infra.celery_app import celery_app, process_session_ingest
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
            db.add(User(id=user_id, clerk_user_id="u_t109", email="t109@t"))
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
                    status="uploaded",
                )
            )
            await db.commit()

    asyncio.run(_seed())

    celery_app.conf.task_always_eager = True
    try:
        yield {
            "db_url": url,
            "factory": factory,
            "session_id": session_id,
        }
    finally:
        celery_app.conf.task_always_eager = False


def test_task_is_registered_on_celery_app() -> None:
    assert "infra.celery_app.process_session_ingest" in celery_app.tasks


def test_task_leaves_status_ready_when_processing_succeeds(
    seeded_session: dict[str, Any],
) -> None:
    # T109 set status="processing" mid-run; T123 then transitions
    # the row to "ready" once the SessionRecord is persisted. End
    # state observed by the caller is "ready".
    session_id = seeded_session["session_id"]
    factory = seeded_session["factory"]
    process_session_ingest.delay(str(session_id))

    async def _read_status() -> str:
        async with factory() as db:
            row = (
                await db.execute(
                    select(SessionRow).where(SessionRow.id == session_id)
                )
            ).scalar_one()
            return str(row.status)

    assert asyncio.run(_read_status()) == "ready"


def test_task_returns_session_id_for_chained_tasks(
    seeded_session: dict[str, Any],
) -> None:
    # T110+ will chain retry / processing tasks; returning the id
    # keeps the chain ergonomic without re-passing args.
    session_id = seeded_session["session_id"]
    result = process_session_ingest.delay(str(session_id))
    assert result.get(timeout=5) == str(session_id)


def test_task_succeeds_with_status_success(
    seeded_session: dict[str, Any],
) -> None:
    result = process_session_ingest.delay(str(seeded_session["session_id"]))
    result.get(timeout=5)
    assert result.successful()


@pytest.mark.usefixtures("seeded_session")
def test_task_raises_when_session_id_unknown() -> None:
    # The task must not silently no-op on an unknown id — that would
    # mask a routing bug. Eager mode propagates the exception via
    # AsyncResult.get().
    bogus = uuid6.uuid7()
    result = process_session_ingest.delay(str(bogus))
    with pytest.raises(Exception):  # noqa: B017
        result.get(timeout=5)
