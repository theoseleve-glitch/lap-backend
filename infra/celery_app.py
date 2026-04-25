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
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog
from celery import Celery, Task
from celery.schedules import crontab
from sqlalchemy import select

from infra.db import make_engine, make_session_factory
from infra.orm import Session as SessionRow
from infra.orm import SessionRecord as SessionRecordRow
from infra.pipeline import run_session_pipeline
from infra.storage import LocalBlobStore

logger = structlog.get_logger()


def build_celery_app() -> Celery:
    """Construct a fresh Celery instance from the current environment.

    Tests call this directly so they can monkeypatch REDIS_URL without
    being defeated by module-level caching.
    """
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    return Celery("lap-backend", broker=redis_url, backend=redis_url)


celery_app = build_celery_app()

# T179 — weekly KPI report posts to Slack every Monday at 09:00 UTC.
# The task body lives below; beat triggers it on the cron schedule.
celery_app.conf.beat_schedule = {
    "weekly-kpi-report": {
        "task": "infra.celery_app.weekly_kpi_report",
        "schedule": crontab(hour=9, minute=0, day_of_week=1),
    },
}
celery_app.conf.timezone = "UTC"


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


async def _mark_ready(session_id: UUID) -> None:
    """T123 final-success marker: status="ready" once processing has
    finished and the SessionRecord row exists.

    Mirrors `_mark_processing`/`_mark_failed`: fresh worker-local
    engine, single update + commit. Raises if the session is missing
    so a stale id surfaces in logs rather than silently no-op'ing.
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
            row.status = "ready"
            await db.commit()
    finally:
        await engine.dispose()


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
    # T124: full pipeline — fetch chunks, parse, detect laps, compute
    # metrics + data quality, persist Lap + SessionRecord rows, mark
    # ready. Worker constructs its own blob store from env vars since
    # it doesn't share the FastAPI dep tree.
    storage_root = Path(os.environ.get("STORAGE_ROOT", "./data"))
    blob_store = LocalBlobStore(storage_root)
    asyncio.run(run_session_pipeline(session_id, blob_store))
    logger.info(
        "process_session_ingest.pipeline_complete",
        session_id=session_id_str,
    )
    return session_id_str


async def _send_weekly_kpi_report() -> None:
    """T179 cron body: compute and post the weekly KPI digest.

    The Slack channel and bot token come from env vars so the worker
    can be configured without code changes. If `SLACK_BOT_TOKEN` is
    absent we log and bail rather than crash the beat scheduler — a
    missing token is an ops issue, not a worker outage.
    """
    from datetime import UTC, datetime

    from infra.kpi_report import send_weekly_kpi_report
    from infra.slack import make_slack_poster

    channel = os.environ.get("SLACK_KPI_CHANNEL", "#ops")
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        logger.warning("weekly_kpi_report.no_slack_token")
        return
    db_url = os.environ["DATABASE_URL"]
    engine = make_engine(db_url)
    try:
        factory = make_session_factory(engine)
        async with factory() as db:
            await send_weekly_kpi_report(
                db,
                make_slack_poster(token),
                channel=channel,
                now=datetime.now(UTC),
            )
    finally:
        await engine.dispose()


@celery_app.task(name="infra.celery_app.weekly_kpi_report")  # type: ignore[untyped-decorator]
def weekly_kpi_report() -> str:
    """Beat-triggered Monday 09:00 UTC. Posts the weekly digest."""
    asyncio.run(_send_weekly_kpi_report())
    return "ok"
