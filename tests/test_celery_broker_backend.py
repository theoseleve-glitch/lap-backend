"""T108 — broker + result backend wired so a task completes visibly.

T107 stood up the Celery app; T108 proves wiring is real by
dispatching a registered task and reading its result back through
the result backend. We use Celery's eager mode so tests don't need
a running Redis — `task_always_eager=True` runs the task in-process
and stores the result in `EagerResult`, which exercises the same
AsyncResult interface a real backend would surface.
"""

import pytest

from infra.celery_app import build_celery_app, celery_app, ping


def test_ping_task_is_registered_on_app() -> None:
    # Tasks dispatched via `.delay()` must be registered on the app
    # the worker loads — otherwise the worker would see them as
    # unknown and reject them.
    assert "infra.celery_app.ping" in celery_app.tasks


def test_dispatch_returns_async_result_with_id() -> None:
    celery_app.conf.task_always_eager = True
    try:
        result = ping.delay()
        assert result.id is not None
    finally:
        celery_app.conf.task_always_eager = False


def test_task_result_is_pong_after_completion() -> None:
    celery_app.conf.task_always_eager = True
    try:
        result = ping.delay()
        # In eager mode `.get()` returns the task's return value
        # synchronously through the same AsyncResult API a real broker
        # round-trip would use.
        assert result.get(timeout=5) == "pong"
    finally:
        celery_app.conf.task_always_eager = False


def test_async_result_status_is_success_after_get() -> None:
    celery_app.conf.task_always_eager = True
    try:
        result = ping.delay()
        result.get(timeout=5)
        assert result.successful()
        assert result.status == "SUCCESS"
    finally:
        celery_app.conf.task_always_eager = False


def test_broker_and_backend_both_present_on_built_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://wired.test:6379/9")
    app = build_celery_app()
    assert app.conf.broker_url
    assert app.conf.result_backend
    # Both endpoints come from the same env var to keep ops simple
    # — operators set REDIS_URL once and the worker reads + writes.
    assert app.conf.broker_url == app.conf.result_backend
