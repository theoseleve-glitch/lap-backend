"""Celery application factory (T107).

CLI:
    celery -A infra.celery_app:celery_app worker -l info

Broker and result backend default to REDIS_URL (matching the rest of
the stack). T108 will tighten broker/result-backend wiring;
T109+ will register actual tasks here.
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
