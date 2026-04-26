"""T087 (sprint2-backend) — calendar refresh cron Mondays 08:00 UTC.

Wires T085 (scraper/upsert) + T086 (parser) into a Celery beat
schedule. The cron body composes:
  raw = await fetch_raw(base_url)
  entries = parse_iracing_calendar_payload(raw)
  count = await store_iracing_calendar_entries(db, entries, now)

Why Monday 08:00 UTC: iRacing typically rolls weekly schedules
on Tuesdays UTC, so Monday gives us the previous week's final
state. Existing 04:00 cohort refresh and 08:00 session-loss audit
keep this slot from contending with quiet-window sweeps.

Tests pin:
  - the beat schedule entry exists at crontab(8,0,Mon)
  - the schedule's task name resolves to a registered task
    (catches typos that would silently fail at fire time)
  - the orchestrator (refresh_iracing_calendar) end-to-end:
    raw payload → parsed entries → rows persisted
  - missing IRACING_CALENDAR_URL env var → log + bail with 0
    (mirrors the kpi_report no-token pattern; ops misconfig
    must not crash the worker)
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic.config import Config as AlembicConfig
from celery.schedules import crontab
from sqlalchemy import select

from alembic import command
from infra.celery_app import (
    celery_app,
    weekly_iracing_calendar_refresh,
)
from infra.db import make_engine, make_session_factory
from infra.iracing_calendar import refresh_iracing_calendar
from infra.orm import IRacingCalendarEntry

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def upgraded_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


def _raw_payload() -> dict[str, Any]:
    return {
        "series": [
            {
                "series_id": 12345,
                "series_name": "Porsche Cup 2026 Season 2",
                "car_classes": [{"name": "Porsche 911 Cup"}],
                "schedules": [
                    {
                        "race_week_num": 5,
                        "track": {
                            "track_id": 200,
                            "track_name": "Spa-Francorchamps",
                            "config_name": "GP",
                        },
                        "start_date": "2026-05-04T00:00:00+00:00",
                        "end_date": "2026-05-10T23:59:59+00:00",
                    },
                ],
            }
        ],
        "special_events": [
            {
                "event_id": 9999,
                "event_name": "Daytona 24 Hours 2026",
                "track": {
                    "track_id": 300,
                    "track_name": "Daytona International Speedway",
                    "config_name": "24h",
                },
                "start_at": "2026-12-12T18:00:00+00:00",
                "end_at": "2026-12-13T18:00:00+00:00",
            }
        ],
    }


def _make_transport(payload: dict[str, Any]) -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)
    return httpx.MockTransport(handler)


# -- beat schedule pins --------------------------------------------


def test_beat_schedule_has_iracing_calendar_refresh_at_monday_0800() -> None:
    """The cron deliverable is "Mondays 08:00 UTC ⇒ runs". Pin
    the schedule entry shape exactly: hour=8, minute=0,
    day_of_week=1 (Monday). A regression that drifts to a
    different day or time would silently miss the iRacing
    weekly schedule rollover."""
    schedule = celery_app.conf.beat_schedule
    assert "weekly-iracing-calendar-refresh" in schedule, (
        f"missing schedule entry; have: {sorted(schedule)}"
    )
    entry = schedule["weekly-iracing-calendar-refresh"]
    expected = crontab(hour=8, minute=0, day_of_week=1)
    assert entry["schedule"] == expected, (
        f"expected Monday 08:00 UTC; got {entry['schedule']!r}"
    )


def test_beat_schedule_task_name_is_registered() -> None:
    """The schedule's `task` string must resolve to a registered
    task at worker startup. A typo here fails silently — beat
    keeps trying to dispatch a non-existent name. Pin: the name
    in the schedule equals the actual task's registered name."""
    schedule = celery_app.conf.beat_schedule
    entry = schedule["weekly-iracing-calendar-refresh"]
    assert entry["task"] in celery_app.tasks, (
        f"task '{entry['task']}' not registered; "
        f"registered tasks: {sorted(celery_app.tasks)}"
    )


# -- orchestrator end-to-end --------------------------------------


def test_refresh_iracing_calendar_persists_rows_from_raw_payload(
    upgraded_db_url: str,
) -> None:
    """Compose fetch_raw → parse → store end-to-end. Mock
    transport returns a raw iRacing-shape payload (1 series-week
    + 1 special). Pin: 2 rows materialized with the source_ids
    derived by the parser."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            transport = _make_transport(_raw_payload())
            now = datetime(2026, 4, 27, 8, 0, tzinfo=UTC)
            async with factory() as db:
                count = await refresh_iracing_calendar(
                    db=db,
                    base_url="https://members-ng.iracing.test",
                    transport=transport,
                    now=now,
                )
                assert count == 2
                rows = (
                    await db.execute(
                        select(IRacingCalendarEntry).order_by(
                            IRacingCalendarEntry.source_id
                        )
                    )
                ).scalars().all()
                source_ids = {r.source_id for r in rows}
                assert source_ids == {
                    "ir-series-12345-week-5",
                    "ir-special-9999",
                }
        finally:
            await engine.dispose()

    asyncio.run(run())


# -- task body resolves the orchestrator --------------------------


def test_celery_task_resolves_url_from_env_and_returns_count(
    upgraded_db_url: str,  # noqa: ARG001  # fixture sets env+migrates
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Celery task body reads IRACING_CALENDAR_URL from env,
    fetches via httpx (real transport — but the test redirects
    to a local MockTransport via a monkeypatch on the helper),
    parses, stores, returns "ok=<n>". Pin: env-var-driven
    configuration so deployments can flip the URL without
    re-deploying code."""
    monkeypatch.setenv(
        "IRACING_CALENDAR_URL", "https://members-ng.iracing.test"
    )
    transport = _make_transport(_raw_payload())
    # Patch at the celery_app module so the task body's
    # transport=transport injection is observed.
    import infra.celery_app as celery_app_module
    monkeypatch.setattr(
        celery_app_module,
        "_make_iracing_transport",
        lambda: transport,
        raising=False,
    )
    result = weekly_iracing_calendar_refresh()
    assert result.startswith("ok=")
    # Two entries from the synthetic raw payload (1 week + 1 special)
    assert result == "ok=2", f"unexpected result: {result}"


def test_celery_task_with_missing_url_logs_and_returns_skipped(
    upgraded_db_url: str,  # noqa: ARG001  # fixture sets env+migrates
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When IRACING_CALENDAR_URL is unset, the task must not
    crash beat — it logs and returns "skipped". Mirrors the
    kpi_report no-token pattern: missing config is an ops issue,
    not a worker outage."""
    monkeypatch.delenv("IRACING_CALENDAR_URL", raising=False)
    result = weekly_iracing_calendar_refresh()
    assert result == "skipped"
