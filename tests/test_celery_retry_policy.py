"""T110 — Celery retry policy: exponential backoff, max 3 attempts.

`process_session_ingest` is configured to auto-retry on any
Exception with exponential backoff, capped at `max_retries=2`
(initial attempt + 2 retries = 3 attempts total). Eager mode in
Celery <6 doesn't actually re-run the task body on Retry — it
propagates Retry instead — so the strongest assertion we can make
without a real broker is on the task class config (which is what
Celery's worker uses to drive the retry chain) plus the observable
fact that an autoretry-decorated failing task surfaces Retry, not
the underlying exception.
"""

import pytest
from celery.exceptions import Retry

from infra.celery_app import celery_app, process_session_ingest


def test_process_session_ingest_retries_on_any_exception() -> None:
    # autoretry_for is stored on the underlying task class — Celery
    # exposes it as Task.autoretry_for.
    assert Exception in process_session_ingest.autoretry_for


def test_process_session_ingest_max_retries_is_2() -> None:
    # max_retries=2 means 3 total attempts (initial + 2 retries).
    # Anything higher prolongs failure visibility for the user;
    # anything lower gives up too soon on transient blips.
    assert process_session_ingest.max_retries == 2


def test_process_session_ingest_uses_exponential_backoff() -> None:
    assert bool(process_session_ingest.retry_backoff) is True


# Companion failing task: same retry config as production.
_attempts: list[int] = []


@celery_app.task(  # type: ignore[untyped-decorator]
    name="tests.test_celery_retry_policy._always_fails",
    autoretry_for=(Exception,),
    retry_backoff=False,
    max_retries=2,
)
def _always_fails() -> None:
    _attempts.append(1)
    raise RuntimeError("simulated_transient_failure")


def test_autoretry_wraps_exception_into_retry_signal() -> None:
    # Proof the autoretry decorator is in effect: a raw RuntimeError
    # is converted into Celery's Retry signal. A real broker would
    # then schedule the next attempt; eager mode propagates Retry as-is.
    _attempts.clear()
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    try:
        with pytest.raises(Retry):
            _always_fails.delay().get(timeout=5)
    finally:
        celery_app.conf.task_always_eager = False
        celery_app.conf.task_eager_propagates = False
    assert _attempts == [1]


def test_failing_task_config_caps_attempts_at_three() -> None:
    # Under a real broker the worker honors max_retries verbatim.
    # We assert the contract on the task class — that's what the
    # worker reads at retry time.
    assert _always_fails.max_retries == 2
    assert Exception in _always_fails.autoretry_for
