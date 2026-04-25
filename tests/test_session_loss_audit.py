"""T180 — daily session-loss audit cron.

`audit_stuck_sessions` returns rows where status="recording" and the
session has been sitting longer than the threshold (default 24h).
`send_session_loss_alert` runs the audit and posts to Slack only
when there's something to flag — silent days don't generate noise.
The Celery beat schedule fires the task once per day.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import uuid6
from alembic.config import Config as AlembicConfig

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.kpi_report import SlackPoster
from infra.orm import DriverProfile, Session, User
from infra.session_audit import (
    audit_stuck_sessions,
    send_session_loss_alert,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


class RecordingSlack(SlackPoster):
    def __init__(self) -> None:
        self.posts: list[tuple[str, str]] = []

    def post(self, channel: str, text: str) -> None:
        self.posts.append((channel, text))


@pytest.fixture
def upgraded_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


async def _seed_session(
    factory: Any, *, status: str, created_age_hours: float
) -> None:
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    now = datetime.now(UTC)
    async with factory() as db:
        db.add(
            User(
                id=user_id,
                clerk_user_id=f"u_{user_id.hex}",
                email=f"{user_id.hex}@test",
            )
        )
        db.add(
            DriverProfile(
                id=driver_id, user_id=user_id, display_name="A"
            )
        )
        db.add(
            Session(
                id=uuid6.uuid7(),
                driver_id=driver_id,
                manifest={},
                status=status,
                created_at=now - timedelta(hours=created_age_hours),
            )
        )
        await db.commit()


async def test_audit_returns_empty_when_no_stuck_sessions(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        factory = make_session_factory(engine)
        # Recently uploaded — not stuck.
        await _seed_session(
            factory, status="ready", created_age_hours=1
        )
        async with factory() as db:
            stuck = await audit_stuck_sessions(
                db, now=datetime.now(UTC)
            )
        assert stuck == []
    finally:
        await engine.dispose()


async def test_audit_finds_recording_session_older_than_24h(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        factory = make_session_factory(engine)
        # 30h old in "recording" — that's a stuck client.
        await _seed_session(
            factory, status="recording", created_age_hours=30
        )
        async with factory() as db:
            stuck = await audit_stuck_sessions(
                db, now=datetime.now(UTC)
            )
        assert len(stuck) == 1
        assert stuck[0].status == "recording"
    finally:
        await engine.dispose()


async def test_audit_excludes_recent_recording_sessions(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        factory = make_session_factory(engine)
        # 6h old in "recording" — still legitimately uploading.
        await _seed_session(
            factory, status="recording", created_age_hours=6
        )
        async with factory() as db:
            stuck = await audit_stuck_sessions(
                db, now=datetime.now(UTC)
            )
        assert stuck == []
    finally:
        await engine.dispose()


async def test_audit_excludes_old_non_recording_sessions(
    upgraded_db_url: str,
) -> None:
    """An old `ready` session is fine — it finished. An old `failed`
    session already alerted via T111. Only `recording`-stuck flags."""
    engine = make_engine(upgraded_db_url)
    try:
        factory = make_session_factory(engine)
        await _seed_session(
            factory, status="ready", created_age_hours=72
        )
        await _seed_session(
            factory, status="failed", created_age_hours=72
        )
        async with factory() as db:
            stuck = await audit_stuck_sessions(
                db, now=datetime.now(UTC)
            )
        assert stuck == []
    finally:
        await engine.dispose()


async def test_alert_posts_to_slack_when_stuck_sessions_found(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        factory = make_session_factory(engine)
        await _seed_session(
            factory, status="recording", created_age_hours=48
        )
        slack = RecordingSlack()
        async with factory() as db:
            await send_session_loss_alert(
                db,
                slack,
                channel="#alerts",
                now=datetime.now(UTC),
            )
        assert len(slack.posts) == 1
        channel, text = slack.posts[0]
        assert channel == "#alerts"
        # Operator needs the count and the word "stuck" or
        # "recording" to grep on at a glance.
        assert "1" in text
        assert "recording" in text.lower() or "stuck" in text.lower()
    finally:
        await engine.dispose()


async def test_alert_silent_when_no_stuck_sessions(
    upgraded_db_url: str,
) -> None:
    """Quiet days = no Slack post. Otherwise the channel becomes
    noise the operator learns to ignore."""
    engine = make_engine(upgraded_db_url)
    try:
        factory = make_session_factory(engine)
        await _seed_session(
            factory, status="ready", created_age_hours=1
        )
        slack = RecordingSlack()
        async with factory() as db:
            await send_session_loss_alert(
                db,
                slack,
                channel="#alerts",
                now=datetime.now(UTC),
            )
        assert slack.posts == []
    finally:
        await engine.dispose()


def test_celery_beat_schedule_has_daily_session_audit_entry() -> None:
    from celery.schedules import crontab

    from infra.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule
    assert "daily-session-loss-audit" in schedule
    entry = schedule["daily-session-loss-audit"]
    cron = entry["schedule"]
    assert isinstance(cron, crontab)
    # Daily — every day-of-week is fine, hour fixed.
    assert cron.minute == {0}
    # Single hour value — keep cadence predictable for the operator.
    assert len(cron.hour) == 1
