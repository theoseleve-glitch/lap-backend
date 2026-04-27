"""T196 (sprint2-backend) — data retention sweep.

Opted-out drivers (cohort_opt_in=False) keep their telemetry
sessions for at most 2 years; after that the cron sweep
anonymizes the rows so personal lap data no longer persists
linked to the driver_id.

Why anonymize and not hard-delete? `sessions.driver_id` is
NOT NULL with FK CASCADE to driver_profiles — we can't sever
the link without a schema change. Replacing the manifest with
a stable stub (`{"anonymized": True}`) achieves the spirit of
the policy: the personal telemetry content is gone, but the
row remains so cohort-aggregate counts stay intact.

Pinned behaviors:
  - sessions <2y old: never touched (opt-in or opt-out)
  - sessions >2y old + opt-OUT: manifest scrubbed
  - sessions >2y old + opt-IN: NEVER touched (the user consented
    to cohort use; their data is the basis for benchmark
    profiles)
  - cross-driver isolation: anonymizing one user's old sessions
    leaves another user's untouched
  - idempotent: running twice scrubs 0 new rows the second time
  - cron wired in infra.celery_app.beat_schedule
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from infra.data_retention import (
    ANONYMIZED_MANIFEST,
    DEFAULT_RETENTION_DAYS,
    anonymize_old_sessions,
)
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, User
from infra.orm import Session as SessionRow

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def db_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[async_sessionmaker[AsyncSession], None, None]:
    """Sync fixture so alembic's internal asyncio.run doesn't
    collide with pytest-asyncio's loop. Tests open and close
    their own AsyncSession instances via the returned factory."""
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    engine = make_engine(url)
    yield make_session_factory(engine)


async def _make_driver(
    db: AsyncSession,
    *,
    clerk_id: str,
    cohort_opt_in: bool,
) -> uuid6.UUID:
    user = User(
        id=uuid6.uuid7(),
        clerk_user_id=clerk_id,
        email=f"{clerk_id}@t.io",
    )
    db.add(user)
    await db.flush()
    profile = DriverProfile(
        id=uuid6.uuid7(),
        user_id=user.id,
        display_name=clerk_id,
        cohort_opt_in=cohort_opt_in,
    )
    db.add(profile)
    await db.flush()
    return profile.id


async def _make_session(
    db: AsyncSession,
    *,
    driver_id: uuid6.UUID,
    created_at: datetime,
    manifest: dict[str, Any] | None = None,
) -> uuid6.UUID:
    s = SessionRow(
        id=uuid6.uuid7(),
        driver_id=driver_id,
        manifest=manifest or {"track": "spa", "car": "gt3"},
    )
    db.add(s)
    await db.flush()
    # Bypass server_default to set deterministic ages.
    s.created_at = created_at
    await db.flush()
    return s.id


# -- defaults ---------------------------------------------------


def test_default_retention_is_two_years() -> None:
    """The 2-year window is what the policy in the sprint task
    promises. Pin the constant so a future "let's tune retention
    to 18 months" PR fails this test until the policy is updated
    in lockstep."""
    assert DEFAULT_RETENTION_DAYS == 730, (
        "2 years = 730 days; mismatch breaks the documented "
        "data-retention policy"
    )


def test_anonymized_manifest_is_stable_marker() -> None:
    """The replacement value is the exact dict re-importers and
    analytics consumers branch on. A regression here (e.g. the
    marker becoming `{"anonymous": True}`) would silently break
    every downstream tool."""
    assert ANONYMIZED_MANIFEST == {"anonymized": True}


# -- core selection logic ---------------------------------------


def test_old_session_for_opted_out_driver_is_anonymized(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The case the policy exists for: opted-out driver, session
    older than 2 years. After the sweep, manifest is the stable
    anonymized stub."""
    now = datetime(2026, 4, 27, tzinfo=UTC)

    async def run() -> None:
        async with db_factory() as db:
            drv = await _make_driver(
                db, clerk_id="t196_opt_out", cohort_opt_in=False
            )
            sid = await _make_session(
                db,
                driver_id=drv,
                created_at=now - timedelta(days=800),  # >2y
            )
            await db.commit()

            n = await anonymize_old_sessions(db, now=now)
            assert n == 1, f"expected 1 row anonymized, got {n}"

        async with db_factory() as db2:
            refreshed = await db2.get(SessionRow, sid)
            assert refreshed is not None
            assert refreshed.manifest == ANONYMIZED_MANIFEST

    asyncio.run(run())


def test_recent_session_for_opted_out_driver_is_left_alone(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An opted-out driver's session that's still inside the 2y
    window must NOT be touched. This is the most common case
    (most data is recent), so a regression here would
    indiscriminately wipe most of the cohort's recent uploads."""
    now = datetime(2026, 4, 27, tzinfo=UTC)
    original = {"track": "monza", "car": "lmp1"}

    async def run() -> None:
        async with db_factory() as db:
            drv = await _make_driver(
                db, clerk_id="t196_recent", cohort_opt_in=False
            )
            sid = await _make_session(
                db,
                driver_id=drv,
                created_at=now - timedelta(days=400),  # <2y
                manifest=original,
            )
            await db.commit()

            n = await anonymize_old_sessions(db, now=now)
            assert n == 0

        async with db_factory() as db2:
            refreshed = await db2.get(SessionRow, sid)
            assert refreshed is not None
            assert refreshed.manifest == original

    asyncio.run(run())


def test_old_session_for_opted_in_driver_is_left_alone(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Opt-in drivers consented to having their data feed cohort
    benchmarks indefinitely (cohort_refresh_weekly relies on it).
    The sweep MUST exclude them — touching their data would
    silently degrade benchmarks for everyone."""
    now = datetime(2026, 4, 27, tzinfo=UTC)
    original = {"track": "imola", "consented": True}

    async def run() -> None:
        async with db_factory() as db:
            drv = await _make_driver(
                db, clerk_id="t196_opt_in", cohort_opt_in=True
            )
            sid = await _make_session(
                db,
                driver_id=drv,
                created_at=now - timedelta(days=900),  # >2y
                manifest=original,
            )
            await db.commit()

            n = await anonymize_old_sessions(db, now=now)
            assert n == 0, (
                "an opt-IN driver's old session was anonymized "
                "— this is the regression that silently corrupts "
                "the cohort benchmark inputs"
            )

        async with db_factory() as db2:
            refreshed = await db2.get(SessionRow, sid)
            assert refreshed is not None
            assert refreshed.manifest == original

    asyncio.run(run())


def test_returns_count_of_rows_touched(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The cron task body logs this count for ops visibility.
    A regression that returns None or 0-when-1 makes the log
    line useless."""
    now = datetime(2026, 4, 27, tzinfo=UTC)

    async def run() -> int:
        async with db_factory() as db:
            drv = await _make_driver(
                db, clerk_id="t196_count", cohort_opt_in=False
            )
            for offset in (800, 850, 900):
                await _make_session(
                    db,
                    driver_id=drv,
                    created_at=now - timedelta(days=offset),
                )
            await db.commit()
            return await anonymize_old_sessions(db, now=now)

    n = asyncio.run(run())
    assert n == 3


def test_idempotent_on_second_run(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Cron jobs miss windows; double-fires happen on retry. The
    second run on the same data must report 0 (already-
    anonymized rows are not "newly" anonymized)."""
    now = datetime(2026, 4, 27, tzinfo=UTC)

    async def run() -> tuple[int, int]:
        async with db_factory() as db:
            drv = await _make_driver(
                db, clerk_id="t196_idempotent", cohort_opt_in=False
            )
            await _make_session(
                db,
                driver_id=drv,
                created_at=now - timedelta(days=800),
            )
            await db.commit()
            first = await anonymize_old_sessions(db, now=now)
            second = await anonymize_old_sessions(db, now=now)
            return first, second

    first, second = asyncio.run(run())
    assert first == 1
    assert second == 0, (
        "second run claimed it anonymized rows that were already "
        "anonymized — log will mislead ops"
    )


def test_does_not_touch_other_drivers_sessions(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """If user A is opted-out with old sessions, user B (opt-in,
    old session) must remain untouched. The sweep filter has to
    match per-driver, not per-row-blindly."""
    now = datetime(2026, 4, 27, tzinfo=UTC)

    async def run() -> tuple[int, uuid6.UUID]:
        async with db_factory() as db:
            a = await _make_driver(
                db, clerk_id="t196_a_optout", cohort_opt_in=False
            )
            b = await _make_driver(
                db, clerk_id="t196_b_optin", cohort_opt_in=True
            )
            await _make_session(
                db,
                driver_id=a,
                created_at=now - timedelta(days=800),
            )
            b_sid = await _make_session(
                db,
                driver_id=b,
                created_at=now - timedelta(days=900),
                manifest={"track": "monaco"},
            )
            await db.commit()
            n = await anonymize_old_sessions(db, now=now)
            return n, b_sid

    n, b_sid = asyncio.run(run())
    assert n == 1, "should sweep only A's session"

    async def check_b() -> dict[str, Any] | None:
        async with db_factory() as db2:
            row = await db2.get(SessionRow, b_sid)
            return row.manifest if row is not None else None

    assert asyncio.run(check_b()) == {"track": "monaco"}


def test_boundary_exactly_at_retention_window(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A session created exactly retention_days ago must NOT be
    swept (the policy says ">2 years"); strictly older does.
    Off-by-one here means we either over-delete (regulator-
    safe but breaks user expectations) or under-delete (policy
    violation)."""
    now = datetime(2026, 4, 27, tzinfo=UTC)

    async def run() -> tuple[uuid6.UUID, uuid6.UUID]:
        async with db_factory() as db:
            drv = await _make_driver(
                db, clerk_id="t196_boundary", cohort_opt_in=False
            )
            boundary_sid = await _make_session(
                db,
                driver_id=drv,
                created_at=now - timedelta(days=DEFAULT_RETENTION_DAYS),
            )
            older_sid = await _make_session(
                db,
                driver_id=drv,
                created_at=now
                - timedelta(days=DEFAULT_RETENTION_DAYS, seconds=1),
            )
            await db.commit()
            await anonymize_old_sessions(db, now=now)
            return boundary_sid, older_sid

    boundary_sid, older_sid = asyncio.run(run())

    async def check() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        async with db_factory() as db2:
            b = await db2.get(SessionRow, boundary_sid)
            o = await db2.get(SessionRow, older_sid)
            return (b.manifest if b else None, o.manifest if o else None)

    boundary_manifest, older_manifest = asyncio.run(check())
    assert boundary_manifest is not None
    assert older_manifest is not None
    assert boundary_manifest != ANONYMIZED_MANIFEST, (
        "session at exact retention boundary was swept; policy "
        "is strictly older-than"
    )
    assert older_manifest == ANONYMIZED_MANIFEST


# -- cron wiring ------------------------------------------------


def test_cron_task_registered_in_beat_schedule() -> None:
    """The function above is dead code unless beat triggers it.
    Pin that infra.celery_app's beat_schedule has an entry whose
    `task` resolves to the data-retention sweep."""
    from infra.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule
    matching = [
        entry
        for entry in schedule.values()
        if "data_retention" in str(entry.get("task", ""))
        or "anonymize" in str(entry.get("task", ""))
    ]
    assert matching, (
        "no beat_schedule entry runs the data-retention sweep; "
        f"existing tasks: {[e.get('task') for e in schedule.values()]}"
    )


def test_cron_task_callable_exists_on_celery_app() -> None:
    """The beat entry's `task` string must resolve to a real
    @celery_app.task. Without this, beat fires but Celery can't
    dispatch."""
    from infra import celery_app as celery_module

    assert hasattr(celery_module, "weekly_data_retention_anonymize"), (
        "celery_app module must expose "
        "weekly_data_retention_anonymize as a @celery_app.task"
    )
