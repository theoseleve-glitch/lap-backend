"""In-process pub/sub for ingest job lifecycle events (T113).

Workers `publish()` events as the pipeline progresses; the SSE
endpoint `subscribe()`s per request and forwards events to the
client. v1 is in-memory and process-local — fine for single-worker
dev. A Redis-pub/sub-backed implementation (later sprint) will
satisfy the same protocol so the SSE handler doesn't change.

`subscribe()` returns an `asyncio.Queue` pre-loaded with any prior
events (deterministic replay on reconnect) and joined to the live
publish stream. Returning the queue (rather than an async generator)
means consumers can `wait_for(queue.get(), timeout=...)` for
keepalives without cancelling the subscription.
"""

import asyncio
import contextlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol


class JobEventBusProtocol(Protocol):
    def publish(
        self, job_id: str, event_name: str, data: dict[str, Any]
    ) -> None: ...

    def subscribe(
        self, job_id: str
    ) -> "asyncio.Queue[tuple[str, dict[str, Any]]]": ...

    def unsubscribe(
        self,
        job_id: str,
        queue: "asyncio.Queue[tuple[str, dict[str, Any]]]",
    ) -> None: ...


class InMemoryJobEventBus:
    def __init__(self) -> None:
        self._buffer: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        self._queues: dict[
            str, list[asyncio.Queue[tuple[str, dict[str, Any]]]]
        ] = {}

    def publish(
        self, job_id: str, event_name: str, data: dict[str, Any]
    ) -> None:
        if not event_name or not event_name.strip():
            raise ValueError("event_name must be non-empty")
        entry = (event_name, data)
        self._buffer.setdefault(job_id, []).append(entry)
        for q in self._queues.get(job_id, []):
            q.put_nowait(entry)

    def subscribe(
        self, job_id: str
    ) -> asyncio.Queue[tuple[str, dict[str, Any]]]:
        queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        # Pre-load with replay snapshot — subscribers always start
        # from the first event, even when they connect mid-flight.
        for entry in list(self._buffer.get(job_id, [])):
            queue.put_nowait(entry)
        self._queues.setdefault(job_id, []).append(queue)
        return queue

    def unsubscribe(
        self,
        job_id: str,
        queue: asyncio.Queue[tuple[str, dict[str, Any]]],
    ) -> None:
        if job_id in self._queues:
            with contextlib.suppress(ValueError):
                self._queues[job_id].remove(queue)


@contextmanager
def subscription(
    bus: JobEventBusProtocol, job_id: str
) -> Iterator[asyncio.Queue[tuple[str, dict[str, Any]]]]:
    """Convenience context manager that pairs subscribe with cleanup."""
    queue = bus.subscribe(job_id)
    try:
        yield queue
    finally:
        bus.unsubscribe(job_id, queue)
