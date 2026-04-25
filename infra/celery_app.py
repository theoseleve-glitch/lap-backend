"""Celery application factory (T107, T108, T109, T110, T111).

CLI:
    celery -A infra.celery_app:celery_app worker -l info

Broker and result backend default to REDIS_URL. `ping` (T108) is the
broker-roundtrip smoke test. `process_session_ingest` (T109) marks
the session "processing"; T110 adds retry; T111 adds an on_failure
handler that flips the row to "failed" with the exception text so
the UI / SSE consumer sees what broke.
"""

import asyncio
import os
from typing import Any
from uuid import UUID

import structlog
from celery import Celery, Task
from sqlalchemy import select

from infra.db import make_engine, make_session_factory
from infra.orm import Session as SessionRow
from infra.orm import SessionRecord as SessionRecordRow

logger = structlog.get_logger()


def build_celery_app() -> Celery:
    """Construct a fresh Celery instance from the current environment.

    Tests call this directly so they can monkeypatch REDIS_URL without
    being defeated by module-level caching.
    """
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    return Celery("lap-backend", broker=redis_url, backend=redis_url)


celery_app = build_celery_app()


@celery_app.task(name="infra.celery_app.ping")  # type: ignore[untyped-decorator]
def ping() -> str:
    """Smoke task — proves the worker loads, dispatches, and returns
    a result through the configured backend. Used by T108 tests."""
    return "pong"


async def _mark_failed(session_id: UUID, reason: str) -> None:
    """T111 final-failure marker: status="failed" + reason text.

    Mirrors `_mark_processing` — fresh worker-local engine, then
    update + commit. Raises if the session is missing so the
    on_failure path of a stale id surfaces in logs rather than
    silently no-op'ing.
    """
    db_url = os.environ["DATABASE_URL"]
    engine = make_engine(db_url)
    try:
        factory = make_session_factory(engine)
        async with factory() as db:
            row = (
                await db.execute(
                    select(SessionRow).where(SessionRow.id == session_id)
                )
            ).scalar_one_or_none()
            if row is None:
                raise ValueError(f"session_not_found: {session_id}")
            row.status = "failed"
            row.failure_reason = reason
            await db.commit()
    finally:
        await engine.dispose()


class SessionIngestTask(Task):  # type: ignore[misc]
    """Custom Task class for ingestion: writes the failure cause back
    to the Session row when retries are exhausted (T111)."""

    def on_failure(
        self,
        exc: BaseException,
        task_id: str,
        args: tuple[Any, ...],
        _kwargs: dict[str, Any],
        _einfo: Any,
    ) -> None:
        if not args:
            logger.warning(
                "process_session_ingest.on_failure.no_args",
                task_id=task_id,
                exc=str(exc),
            )
            return
        try:
            session_id = UUID(str(args[0]))
        except (ValueError, TypeError):
            logger.warning(
                "process_session_ingest.on_failure.bad_session_id",
                task_id=task_id,
                arg=str(args[0]),
            )
            return
        try:
            asyncio.run(_mark_failed(session_id, str(exc)))
        except Exception as mark_exc:  # noqa: BLE001
            # Don't crash the worker shutdown path — log and move on.
            logger.error(
                "process_session_ingest.on_failure.mark_failed_error",
                session_id=str(session_id),
                error=str(mark_exc),
            )


async def _persist_session_record(
    session_id: UUID, payload: dict[str, Any]
) -> None:
    """T122: insert-or-update the SessionRecord row for this session.

    Idempotent: if a row already exists (Celery retry re-enters the
    task body after a downstream failure), the payload is overwritten
    rather than tripping the unique session_id constraint. The FK to
    `sessions.id` is enforced — an unknown session_id raises an
    integrity error rather than silently creating an orphan row.
    """
    db_url = os.environ["DATABASE_URL"]
    engine = make_engine(db_url)
    try:
        factory = make_session_factory(engine)
        async with factory() as db:
            existing = (
                await db.execute(
                    select(SessionRecordRow).where(
                        SessionRecordRow.session_id == session_id
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                db.add(
                    SessionRecordRow(
                        session_id=session_id, payload=payload
                    )
                )
            else:
                existing.payload = payload
            await db.commit()
    finally:
        await engine.dispose()


async def _mark_processing(session_id: UUID) -> None:
    """Open a worker-local DB session and transition the row.

    Celery worker processes don't share the FastAPI dep tree, so the
    task builds its own engine from DATABASE_URL. The async pattern
    matches the rest of the codebase; we wrap with asyncio.run inside
    the sync task body.
    """
    db_url = os.environ["DATABASE_URL"]
    engine = make_engine(db_url)
    try:
        factory = make_session_factory(engine)
        async with factory() as db:
            row = (
                await db.execute(
                    select(SessionRow).where(SessionRow.id == session_id)
                )
            ).scalar_one_or_none()
            if row is None:
                raise ValueError(f"session_not_found: {session_id}")
            row.status = "processing"
            await db.commit()
    finally:
        await engine.dispose()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="infra.celery_app.process_session_ingest",
    base=SessionIngestTask,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=2,
)
def process_session_ingest(session_id_str: str) -> str:
    """T109 skeleton: load manifest, mark processing, log.

    Returns the session_id so downstream chained tasks (T110+) don't
    have to re-thread it through args.
    """
    session_id = UUID(session_id_str)
    logger.info(
        "process_session_ingest.start", session_id=session_id_str
    )
    asyncio.run(_mark_processing(session_id))
    logger.info(
        "process_session_ingest.marked_processing",
        session_id=session_id_str,
    )
    # T122: persist a SessionRecord row at the end of processing.
    # Payload starts empty — T124/T125 integration tests will pin a
    # richer shape once lap detection / metrics / data quality flow
    # end-to-end through the worker.
    asyncio.run(_persist_session_record(session_id, payload={}))
    logger.info(
        "process_session_ingest.session_record_persisted",
        session_id=session_id_str,
    )
    return session_id_str
