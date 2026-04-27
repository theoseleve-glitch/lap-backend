"""T094 (sprint2-backend) — event notification windowing.

Pure-domain function. Given upcoming TargetEvent-shaped
candidates and `now`, returns the (event, window) pairs that
need an email dispatched right now. The Celery hourly beat
calls this and persists the chosen window labels back into
each candidate's `notifications_sent` so re-runs stay silent.

Two windows match the spec ("24h and 1h before"):
  - "24h":  starts_at ∈ (now+23.5h, now+24.5h]
  - "1h" :  starts_at ∈ (now+30m,   now+90m]

Tolerance widths (±30m) match the chosen hourly beat cadence so
each event gets exactly one fire per window — wider would
double-fire on the next hour, narrower could miss the window
on a delayed beat.

`notifications_sent` is checked per-window so an event that
already got the 24h email still receives the 1h email.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

_W24H_LO = timedelta(hours=23, minutes=30)
_W24H_HI = timedelta(hours=24, minutes=30)
_W1H_LO = timedelta(minutes=30)
_W1H_HI = timedelta(minutes=90)


class _NotifiableEvent(Protocol):
    """The shape `compute_due_notifications` reads off each
    candidate. The TargetEvent ORM row matches this naturally;
    tests pass a frozen dataclass with the same names."""

    id: UUID
    driver_id: UUID
    starts_at: datetime
    notifications_sent: Sequence[str]


@dataclass(frozen=True)
class DueNotification:
    """One pending email. The dispatcher reads driver_id to
    resolve the email address and target_event_id to render
    the subject/body."""

    target_event_id: UUID
    driver_id: UUID
    window: str  # "24h" or "1h"


def compute_due_notifications(
    *,
    candidates: Iterable[_NotifiableEvent],
    now: datetime,
) -> list[DueNotification]:
    """Scan `candidates`, return the due (event, window) pairs.

    Per-window idempotency: if a candidate's notifications_sent
    already includes a window label, the function never returns
    that pair. The two windows are evaluated independently.
    """
    due: list[DueNotification] = []
    for ev in candidates:
        delta = ev.starts_at - now
        already = set(ev.notifications_sent or [])
        if "24h" not in already and _W24H_LO < delta <= _W24H_HI:
            due.append(
                DueNotification(
                    target_event_id=ev.id,
                    driver_id=ev.driver_id,
                    window="24h",
                )
            )
        if "1h" not in already and _W1H_LO < delta <= _W1H_HI:
            due.append(
                DueNotification(
                    target_event_id=ev.id,
                    driver_id=ev.driver_id,
                    window="1h",
                )
            )
    return due
