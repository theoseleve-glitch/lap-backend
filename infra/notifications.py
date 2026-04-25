"""Minimal in-process notification queue (T155).

A pluggable indirection so webhook handlers can declare "this user
needs notifying about X" without hard-wiring an email/push backend.
The current `NotificationQueue` just buffers in memory; production
will swap for a Celery-task-backed dispatcher behind the same dep.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Notification:
    kind: str  # "payment_failed", "trial_ending", ...
    user_id: str
    payload: dict[str, Any] = field(default_factory=dict)


class NotificationQueue:
    """In-process FIFO buffer. `drain()` returns and clears the
    pending list — useful for tests and for a future periodic
    flusher that hands work off to a real backend."""

    def __init__(self) -> None:
        self._items: list[Notification] = []

    def enqueue(self, notification: Notification) -> None:
        self._items.append(notification)

    def drain(self) -> list[Notification]:
        out, self._items = self._items, []
        return out

    def __len__(self) -> int:
        return len(self._items)


_default_queue = NotificationQueue()


def get_notification_queue() -> NotificationQueue:
    """FastAPI dependency. Returns the process-wide queue; tests
    override via `app.dependency_overrides` to inject a fresh one."""
    return _default_queue
