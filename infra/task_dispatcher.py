"""Task dispatcher abstraction (T103).

`InMemoryTaskDispatcher` is the v1 implementation — it records what
*would* have been queued. T107 will introduce `CeleryTaskDispatcher`
behind the same `TaskDispatcherProtocol` so the endpoint never
imports Celery directly. Each dispatch returns an opaque `job_id`
that T104 returns to the client for status polling.
"""

from typing import Protocol
from uuid import UUID

import uuid6


class TaskDispatcherProtocol(Protocol):
    def dispatch_session_ingest(self, session_id: UUID) -> str: ...


class InMemoryTaskDispatcher:
    def __init__(self) -> None:
        self.dispatched: list[UUID] = []
        self.last_job_id: str | None = None

    def dispatch_session_ingest(self, session_id: UUID) -> str:
        job_id = str(uuid6.uuid7())
        self.dispatched.append(session_id)
        self.last_job_id = job_id
        return job_id
