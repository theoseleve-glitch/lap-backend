"""In-process pub/sub for ingest job lifecycle events (T113, T114).

Workers `publish()` events as the pipeline progresses; the SSE
endpoint `subscribe()`s per request and forwards events to the
client. v1 is in-memory and process-local — fine for single-worker
dev. A Redis-pub/sub-backed implementation (later sprint) will
satisfy the same protocol so the SSE handler doesn't change.

Each published event is assigned a monotonic, per-job event id
starting at 1. The SSE endpoint emits these ids on the wire so
clients can resume after a reconnect via `Last-Event-ID` (T114).

`subscribe()` returns an `asyncio.Queue` pre-loaded with the
buffered history strictly after `after_id` (deterministic resume on
reconnect) and joined to the live publish stream. Returning the
queue (rather than an async generator) means consumers can
`wait_for(queue.get(), timeout=...)` for keepalives without
cancelling the subscription.
"""

import asyncio
import contextlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol

EventEntry = tuple[int, str, dict[str, Any]]


class JobEventBusProtocol(Protocol):
    def publish(
        self, job_id: str, event_name: str, data: dict[str, Any]
    ) -> None: ...

    def subscribe(
        self, job_id: str, after_id: int | None = None
    ) -> "asyncio.Queue[EventEntry]": ...

    def unsubscribe(
        self,
        job_id: str,
        queue: "asyncio.Queue[EventEntry]",
    ) -> None: ...


class InMemoryJobEventBus:
    def __init__(self) -> None:
        self._buffer: dict[str, list[EventEntry]] = {}
        self._next_id: dict[str, int] = {}
        self._queues: dict[str, list[asyncio.Queue[EventEntry]]] = {}

    def publish(
        self, job_id: str, event_name: str, data: dict[str, Any]
    ) -> None:
        if not event_name or not event_name.strip():
            raise ValueError("event_name must be non-empty")
        next_id = self._next_id.get(job_id, 1)
        entry: EventEntry = (next_id, event_name, data)
        self._next_id[job_id] = next_id + 1
        self._buffer.setdefault(job_id, []).append(entry)
        for q in self._queues.get(job_id, []):
            q.put_nowait(entry)

    def subscribe(
        self, job_id: str, after_id: int | None = None
    ) -> asyncio.Queue[EventEntry]:
        queue: asyncio.Queue[EventEntry] = asyncio.Queue()
        # Replay snapshot, filtered by the resume cursor.
        for entry in list(self._buffer.get(job_id, [])):
            if after_id is None or entry[0] > after_id:
                queue.put_nowait(entry)
        self._queues.setdefault(job_id, []).append(queue)
        return queue

    def unsubscribe(
        self,
        job_id: str,
        queue: asyncio.Queue[EventEntry],
    ) -> None:
        if job_id in self._queues:
            with contextlib.suppress(ValueError):
                self._queues[job_id].remove(queue)


@contextmanager
def subscription(
    bus: JobEventBusProtocol,
    job_id: str,
    after_id: int | None = None,
) -> Iterator[asyncio.Queue[EventEntry]]:
    """Convenience context manager that pairs subscribe with cleanup."""
    queue = bus.subscribe(job_id, after_id=after_id)
    try:
        yield queue
    finally:
        bus.unsubscribe(job_id, queue)
