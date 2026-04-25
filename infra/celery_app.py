"""Celery application factory (T107, T108, T109).

CLI:
    celery -A infra.celery_app:celery_app worker -l info

Broker and result backend default to REDIS_URL. `ping` (T108) is the
broker-roundtrip smoke test. `process_session_ingest` (T109) is the
v1 ingestion-pipeline skeleton — read manifest, mark "processing",
log. T110+ will add retry semantics and the real processing stages.
"""

import asyncio
import os
from uuid import UUID

import structlog
from celery import Celery
from sqlalchemy import select

from infra.db import make_engine, make_session_factory
from infra.orm import Session as SessionRow

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
    return session_id_str
