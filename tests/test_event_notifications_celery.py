"""T094 (sprint2-backend) — Celery wiring for event notifications.

Tests `_run_event_notifications` (the orchestrator the
Celery beat task calls). The pure-domain windowing function
already has its own pin in test_event_notifications_due.py;
this file pins the orchestration: query candidates, dispatch
emails through the injected sender, and persist
`notifications_sent` so the next beat skips already-sent items.

Beat schedule itself (hourly at minute 0) is asserted in
test_celery_beat_schedule.py-style coverage along with the
other cron entries; here we only test the function body.

Pinned behaviors:
  - candidate at T-24h with empty notifications_sent →
    sender called with that driver's email + a "24h" subject;
    `notifications_sent` updated to ["24h"]
  - second invocation with the same state → sender NOT called
    again (idempotency surfaces through DB persistence)
  - candidate outside windows → sender NOT called
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.event_notifications_runner import (
    run_event_notifications,
)
from infra.orm import (
    DriverProfile,
    TargetEvent,
    User,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def session_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[async_sessionmaker[AsyncSession], None, None]:
    db_path = tmp_path / "notif.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    yield factory


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    *,
    starts_at: datetime,
    notifications_sent: list[str] | None = None,
    email: str = "driver@test",
) -> uuid6.UUID:
    """Seed a User + DriverProfile + TargetEvent. Returns the
    target_event id so the test can re-fetch and assert state."""
    async with factory() as db:
        user = User(
            id=uuid6.uuid7(),
            clerk_user_id=f"u-{uuid6.uuid7().hex}",
            email=email,
        )
        driver = DriverProfile(
            id=uuid6.uuid7(), user_id=user.id, display_name="t094",
        )
        event = TargetEvent(
            id=uuid6.uuid7(),
            driver_id=driver.id,
            name="Spa 6h Endurance",
            kind="sim_event",
            starts_at=starts_at,
            competitive_weight=0.7,
            notifications_sent=list(notifications_sent or []),
        )
        db.add_all([user, driver, event])
        await db.commit()
        return event.id


def _make_recorder() -> tuple[list[tuple[str, str, str]], Any]:
    """Return (calls_list, sender_callable). The callable
    appends each call to the list — the test asserts on it."""
    calls: list[tuple[str, str, str]] = []

    def _send(to: str, subject: str, body: str) -> None:
        calls.append((to, subject, body))

    return calls, _send


# -- happy path: 24h reminder fires + persists ------------------


def test_event_at_24h_triggers_email_and_persists_24h_window(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Driver has an event 24h out + nothing sent yet →
    runner dispatches one email AND writes "24h" to
    notifications_sent so the next beat skips it. Pin: this
    is the headline acceptance — both the dispatch AND the
    durability of the idempotency marker."""
    now = datetime.now(UTC)
    starts_at = now + timedelta(hours=24)
    event_id = asyncio.run(
        _seed(
            session_factory,
            starts_at=starts_at,
            email="reminder@test",
        )
    )
    calls, sender = _make_recorder()

    asyncio.run(
        run_event_notifications(
            session_factory=session_factory, sender=sender, now=now,
        )
    )

    assert len(calls) == 1
    to, subject, _body = calls[0]
    assert to == "reminder@test"
    # Subject must signal which window so an audit-log scan can
    # tell 24h vs 1h reminders apart at a glance.
    assert "24h" in subject or "24" in subject

    # Persistence pin: re-read the row, "24h" must be in the
    # notifications_sent list. Without this, the next hourly
    # beat would re-dispatch.
    async def reread() -> list[str]:
        async with session_factory() as db:
            row = (
                await db.execute(
                    select(TargetEvent).where(TargetEvent.id == event_id)
                )
            ).scalar_one()
            return list(row.notifications_sent)

    assert "24h" in asyncio.run(reread())


# -- idempotency: re-running does not re-fire -------------------


def test_second_run_after_24h_sent_does_not_re_fire(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Already-sent state must propagate through. Pin: the
    second hourly beat (one hour after the first) sees an event
    still ~23h out — without idempotency it'd fire again. With
    idempotency (T094's `notifications_sent` filter) it stays
    silent."""
    now = datetime.now(UTC)
    asyncio.run(
        _seed(
            session_factory,
            starts_at=now + timedelta(hours=24),
            notifications_sent=["24h"],
        )
    )
    calls, sender = _make_recorder()
    asyncio.run(
        run_event_notifications(
            session_factory=session_factory, sender=sender, now=now,
        )
    )
    assert calls == []


# -- silence on out-of-window events ----------------------------


def test_event_outside_windows_does_not_fire(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An event 12h out should be silent. Pin: the runner must
    rely on the windowing function and not "always email if
    upcoming" — that would page drivers every hour for days."""
    now = datetime.now(UTC)
    asyncio.run(
        _seed(
            session_factory,
            starts_at=now + timedelta(hours=12),
        )
    )
    calls, sender = _make_recorder()
    asyncio.run(
        run_event_notifications(
            session_factory=session_factory, sender=sender, now=now,
        )
    )
    assert calls == []
