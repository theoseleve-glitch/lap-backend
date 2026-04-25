"""T179 — weekly Slack KPI cron at Monday 09:00 UTC.

`send_weekly_kpi_report` computes the same KPIs as T178's
`/api/v1/admin/kpis` (refactored into a shared helper), formats a
readable report, and posts it via an injected SlackPoster. The
Celery beat schedule fires this task once a week.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import uuid6
from alembic.config import Config as AlembicConfig

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.kpi_report import (
    SlackPoster,
    compute_weekly_kpis,
    format_weekly_report,
    send_weekly_kpi_report,
)
from infra.orm import DriverProfile, Session, User

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


async def _seed(
    factory: Any,
    *,
    user_age_days: int,
    subscription_status: str,
    session_specs: list[tuple[int, str]],
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
                created_at=now - timedelta(days=user_age_days),
                subscription_status=subscription_status,
            )
        )
        db.add(
            DriverProfile(
                id=driver_id, user_id=user_id, display_name="A"
            )
        )
        for age_days, sess_status in session_specs:
            db.add(
                Session(
                    id=uuid6.uuid7(),
                    driver_id=driver_id,
                    manifest={},
                    status=sess_status,
                    created_at=now - timedelta(days=age_days),
                )
            )
        await db.commit()


async def test_compute_weekly_kpis_empty_db(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        factory = make_session_factory(engine)
        async with factory() as db:
            kpis = await compute_weekly_kpis(db, datetime.now(UTC))
        assert kpis["wpd"] == 0
        assert kpis["activation_rate"] == 0.0
        assert kpis["upload_success_rate"] == 1.0
    finally:
        await engine.dispose()


async def test_compute_weekly_kpis_with_data(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        factory = make_session_factory(engine)
        # active driver with recent session → +1 WPD, +1 activated/+1 signup
        await _seed(
            factory,
            user_age_days=2,
            subscription_status="active",
            session_specs=[(1, "ready")],
        )
        # inactive driver, recent session → 0 WPD, +1 activated/+1 signup
        await _seed(
            factory,
            user_age_days=3,
            subscription_status="inactive",
            session_specs=[(1, "failed")],
        )
        async with factory() as db:
            kpis = await compute_weekly_kpis(db, datetime.now(UTC))
        assert kpis["wpd"] == 1
        assert kpis["activation_rate"] == 1.0  # 2 signups, 2 activated
        assert kpis["upload_success_rate"] == 0.5  # 1 ready, 1 failed
    finally:
        await engine.dispose()


def test_format_weekly_report_includes_all_three_kpis() -> None:
    kpis = {
        "wpd": 42,
        "activation_rate": 0.55,
        "upload_success_rate": 0.985,
    }
    week_ending = datetime(2026, 4, 27, tzinfo=UTC)
    text = format_weekly_report(kpis, week_ending)
    # Operator should be able to read the message at a glance —
    # week label + each metric in a recognizable shape.
    assert "2026-04-27" in text
    assert "42" in text  # WPD value
    assert "55" in text  # 55%
    assert "98" in text  # 98.5% — accept either rounding
    assert "WPD" in text or "Weekly Paid Drivers" in text


async def test_send_weekly_kpi_report_posts_to_slack(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        factory = make_session_factory(engine)
        await _seed(
            factory,
            user_age_days=1,
            subscription_status="trialing",
            session_specs=[(1, "ready")],
        )
        slack = RecordingSlack()
        async with factory() as db:
            await send_weekly_kpi_report(
                db,
                slack,
                channel="#ops",
                now=datetime.now(UTC),
            )
        assert len(slack.posts) == 1
        channel, text = slack.posts[0]
        assert channel == "#ops"
        assert "WPD" in text or "Weekly Paid Drivers" in text
    finally:
        await engine.dispose()


def test_celery_beat_schedule_has_monday_09_utc_entry() -> None:
    from celery.schedules import crontab

    from infra.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule
    assert "weekly-kpi-report" in schedule
    entry = schedule["weekly-kpi-report"]
    cron = entry["schedule"]
    assert isinstance(cron, crontab)
    # Mondays at 09:00 UTC: hour=9, minute=0, day_of_week={1}.
    assert cron.hour == {9}
    assert cron.minute == {0}
    # day_of_week semantics differ across celery versions; "1" or
    # "mon" both resolve to weekday 1 (Mon).
    assert 1 in cron.day_of_week
