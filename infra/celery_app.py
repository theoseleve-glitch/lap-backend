"""Celery application factory (T107, T108).

CLI:
    celery -A infra.celery_app:celery_app worker -l info

Broker and result backend default to REDIS_URL (matching the rest of
the stack). T108 adds a `ping` task that proves the broker +
result-backend wiring round-trip cleanly. T109+ will register the
real ingestion tasks here.
"""

import os

from celery import Celery


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
