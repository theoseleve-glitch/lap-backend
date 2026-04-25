"""Unit tests for the task dispatcher (T103).

The dispatcher abstracts queuing of background work so the endpoint
isn't coupled to Celery. T107 will swap the in-memory implementation
for a Celery-backed one without changing callers.
"""

import uuid6

from infra.task_dispatcher import InMemoryTaskDispatcher


def test_dispatch_records_session_id() -> None:
    d = InMemoryTaskDispatcher()
    session_id = uuid6.uuid7()
    d.dispatch_session_ingest(session_id)
    assert d.dispatched == [session_id]


def test_dispatch_returns_job_id_string() -> None:
    d = InMemoryTaskDispatcher()
    job_id = d.dispatch_session_ingest(uuid6.uuid7())
    assert isinstance(job_id, str)
    assert len(job_id) >= 16


def test_distinct_dispatches_get_distinct_job_ids() -> None:
    d = InMemoryTaskDispatcher()
    a = d.dispatch_session_ingest(uuid6.uuid7())
    b = d.dispatch_session_ingest(uuid6.uuid7())
    assert a != b


def test_dispatch_appends_in_call_order() -> None:
    d = InMemoryTaskDispatcher()
    s1 = uuid6.uuid7()
    s2 = uuid6.uuid7()
    s3 = uuid6.uuid7()
    d.dispatch_session_ingest(s1)
    d.dispatch_session_ingest(s2)
    d.dispatch_session_ingest(s3)
    assert d.dispatched == [s1, s2, s3]


def test_dispatch_records_job_id_alongside_session() -> None:
    d = InMemoryTaskDispatcher()
    session_id = uuid6.uuid7()
    job_id = d.dispatch_session_ingest(session_id)
    # The dispatcher should be able to relate the queued task back to
    # its job id — T104 will return both to the client.
    assert d.last_job_id == job_id
