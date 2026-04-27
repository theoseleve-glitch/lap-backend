"""T094 (sprint2-backend) — orchestrator for event email reminders.

Wires together:
  - the pure-domain `compute_due_notifications` (windowing +
    idempotency check)
  - the email sender (injected for testability)
  - the persistence write that appends the dispatched window
    label to `target_events.notifications_sent`

Called by the hourly Celery beat (T094 wiring in
`infra.celery_app`). Test calls it directly with an in-test
session factory and a recording sender.

Lookup window for the DB query: `now <= starts_at <= now + 25h`.
That covers the upper edge of the 24h window (24.5h) with a
small buffer; events further out can't be due yet.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.event_notifications import compute_due_notifications
from infra.email import EmailSender
from infra.orm import DriverProfile, TargetEvent, User

_LOOKUP_HORIZON = timedelta(hours=25)


async def run_event_notifications(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    sender: EmailSender,
    now: datetime | None = None,
) -> int:
    """Find due notifications, dispatch each, persist the
    window label. Returns the count of emails sent (logged
    by the Celery task wrapper for ops visibility).

    Per-row UPDATE pattern (not bulk): each row's
    notifications_sent is read, mutated, written. Bulk
    JSONB-array-append exists in Postgres but isn't
    cross-dialect, and the volume here (one event per driver
    per pin) is too small for the optimization to matter.
    """
    if now is None:
        now = datetime.now(UTC)
    sent = 0
    async with session_factory() as db:
        candidates = await _load_candidates(db, now=now)
        # SQLite tz-strip on cross-object reload: re-tag UTC
        # so the windowing comparison stays consistent across
        # dialects (Postgres preserves tz natively).
        for ev in candidates:
            if ev.starts_at.tzinfo is None:
                ev.starts_at = ev.starts_at.replace(tzinfo=UTC)
        due_list = compute_due_notifications(
            candidates=candidates, now=now,
        )
        if not due_list:
            return 0
        # Resolve emails in one query rather than per-row.
        driver_ids = {d.driver_id for d in due_list}
        emails = await _emails_by_driver(db, driver_ids=driver_ids)
        # Re-fetch as a dict for in-place mutation.
        events_by_id = {ev.id: ev for ev in candidates}
        for due in due_list:
            email = emails.get(due.driver_id)
            if email is None:
                # Driver row missing or email empty — skip silently.
                # CASCADE delete should keep this from happening,
                # but defensive: emailing nobody is preferable to
                # crashing the beat run for everyone.
                continue
            ev = events_by_id[due.target_event_id]
            sender(
                email,
                _subject(ev, due.window),
                _body(ev, due.window),
            )
            # Append the window label so the next beat skips it.
            # Pydantic-side `notifications_sent` is a list; mutate
            # in place AND reassign so SQLAlchemy detects the
            # change on JSON columns (SQLAlchemy doesn't track
            # in-place edits to JSON / JSONB by default).
            updated = list(ev.notifications_sent or [])
            updated.append(due.window)
            ev.notifications_sent = updated
            sent += 1
        await db.commit()
    return sent


async def _load_candidates(
    db: AsyncSession, *, now: datetime,
) -> list[TargetEvent]:
    """Load every TargetEvent whose starts_at falls in the
    lookup horizon. Past events are excluded; events further
    than 25h out can't be due in either window."""
    horizon = now + _LOOKUP_HORIZON
    rows = (
        await db.execute(
            select(TargetEvent).where(
                TargetEvent.starts_at >= now,
                TargetEvent.starts_at <= horizon,
            )
        )
    ).scalars().all()
    return list(rows)


async def _emails_by_driver(
    db: AsyncSession, *, driver_ids: set[Any],
) -> dict[Any, str]:
    """Return {driver_id: email} for the given driver ids.
    One join: DriverProfile.user_id → User.email."""
    if not driver_ids:
        return {}
    rows = (
        await db.execute(
            select(DriverProfile.id, User.email)
            .join(User, User.id == DriverProfile.user_id)
            .where(DriverProfile.id.in_(driver_ids))
        )
    ).all()
    return dict(rows)


def _subject(ev: TargetEvent, window: str) -> str:
    """Subject line carries the window label so audit-log
    grep can distinguish 24h vs 1h reminders without a body
    parse."""
    return f"[LAP] {window} reminder: {ev.name}"


def _body(ev: TargetEvent, window: str) -> str:
    """Body is intentionally plain — formatted UI lives in
    a future template task. The reminder content is just the
    event name + start time + the window label."""
    return (
        f"Heads up: your pinned event '{ev.name}' starts at "
        f"{ev.starts_at.isoformat()} ({window} reminder)."
    )
