"""T223 — Prescription expiry sweep.

Pure function that scans `action_prescriptions` for rows whose
`valid_until` is in the past AND whose status is still in an
actionable state (proposed/accepted/in_progress), and flips
their status to 'expired'.

Closes the lifecycle on the calendar side: T215/T216 are
user-driven terminal transitions, T219 is data-driven (we
measured), T223 is calendar-driven (the actionable window
elapsed). Without it, prescriptions with an explicit deadline
would linger in the "open" list forever as zombie rows.

Idempotent — once a row is 'expired' it's terminal and a second
sweep won't touch it. Same for the other terminals (completed,
skipped). NULL valid_until = no deadline = sweep ignores.

The Celery beat hook (in `infra.celery_app`) wires this to a
nightly run at 03:30 UTC.
"""

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import (
    ActionPrescription,
    DriverProfile,
    User,
    WeaknessEpisode,
)
from infra.prescription_expiry import expire_overdue_prescriptions

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def session_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[async_sessionmaker[AsyncSession], None, None]:
    db_path = tmp_path / "exp.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    yield factory


async def _seed_user_driver_episode(
    db: AsyncSession,
) -> tuple[object, object]:
    """Returns (driver_id, episode_id)."""
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    episode_id = uuid6.uuid7()
    now = datetime.now(UTC)
    db.add(
        User(
            id=user_id,
            clerk_user_id=f"u_exp_{user_id.hex}",
            email=f"exp_{user_id.hex}@test",
        )
    )
    db.add(
        DriverProfile(id=driver_id, user_id=user_id, display_name="A")
    )
    db.add(
        WeaknessEpisode(
            id=episode_id, driver_id=driver_id,
            category="technique", subcategory="late_braking",
            first_observed_at=now, last_observed_at=now,
            recurrence_count=1, sessions_affected=[],
            severity="medium", confidence=1.0, status="open",
            linked_prescriptions=[], linked_insights=[],
            library_rule_id="br.late_braking.apex_speed_loss",
        )
    )
    await db.commit()
    return driver_id, episode_id


async def _add_pres(
    db: AsyncSession,
    *,
    driver_id: object,
    episode_id: object,
    status: str,
    valid_until: datetime | None,
) -> object:
    p_id = uuid6.uuid7()
    now = datetime.now(UTC)
    db.add(
        ActionPrescription(
            id=p_id, driver_id=driver_id,
            weakness_episode_id=episode_id, created_at=now,
            priority="p1", title="t", rationale="r",
            expected_context={}, success_condition={},
            status=status, drill_template_id="drill.x",
            valid_until=valid_until,
        )
    )
    await db.commit()
    return p_id


async def _read_status(db: AsyncSession, p_id: object) -> str:
    return (
        await db.execute(
            select(ActionPrescription.status).where(
                ActionPrescription.id == p_id
            )
        )
    ).scalar_one()


async def test_expires_proposed_prescription_past_valid_until(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    async with session_factory() as db:
        driver, episode = await _seed_user_driver_episode(db)
        p = await _add_pres(
            db, driver_id=driver, episode_id=episode,
            status="proposed",
            valid_until=now - timedelta(days=1),
        )
        updated = await expire_overdue_prescriptions(db, now=now)
        status_after = await _read_status(db, p)
    assert updated == 1
    assert status_after == "expired"


async def test_expires_accepted_and_in_progress_prescriptions(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """All three actionable states are eligible for expiry."""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    past = now - timedelta(hours=1)
    async with session_factory() as db:
        driver, episode = await _seed_user_driver_episode(db)
        p_acc = await _add_pres(
            db, driver_id=driver, episode_id=episode,
            status="accepted", valid_until=past,
        )
        p_ip = await _add_pres(
            db, driver_id=driver, episode_id=episode,
            status="in_progress", valid_until=past,
        )
        updated = await expire_overdue_prescriptions(db, now=now)
        s_acc = await _read_status(db, p_acc)
        s_ip = await _read_status(db, p_ip)
    assert updated == 2
    assert s_acc == "expired"
    assert s_ip == "expired"


async def test_does_not_touch_prescriptions_within_valid_until(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    async with session_factory() as db:
        driver, episode = await _seed_user_driver_episode(db)
        p = await _add_pres(
            db, driver_id=driver, episode_id=episode,
            status="proposed",
            valid_until=now + timedelta(days=1),
        )
        updated = await expire_overdue_prescriptions(db, now=now)
        status_after = await _read_status(db, p)
    assert updated == 0
    assert status_after == "proposed"


async def test_does_not_touch_prescriptions_with_null_valid_until(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No deadline = no expiry. Open-ended prescriptions wait
    for the user or the outcome checker to terminate them."""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    async with session_factory() as db:
        driver, episode = await _seed_user_driver_episode(db)
        p = await _add_pres(
            db, driver_id=driver, episode_id=episode,
            status="proposed", valid_until=None,
        )
        updated = await expire_overdue_prescriptions(db, now=now)
        status_after = await _read_status(db, p)
    assert updated == 0
    assert status_after == "proposed"


async def test_does_not_re_terminalize_completed_skipped_or_expired(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Terminal statuses are sticky — sweep skips them."""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    past = now - timedelta(days=1)
    async with session_factory() as db:
        driver, episode = await _seed_user_driver_episode(db)
        p_done = await _add_pres(
            db, driver_id=driver, episode_id=episode,
            status="completed", valid_until=past,
        )
        p_skip = await _add_pres(
            db, driver_id=driver, episode_id=episode,
            status="skipped", valid_until=past,
        )
        p_exp = await _add_pres(
            db, driver_id=driver, episode_id=episode,
            status="expired", valid_until=past,
        )
        updated = await expire_overdue_prescriptions(db, now=now)
        s_done = await _read_status(db, p_done)
        s_skip = await _read_status(db, p_skip)
        s_exp = await _read_status(db, p_exp)
    assert updated == 0
    assert s_done == "completed"
    assert s_skip == "skipped"
    assert s_exp == "expired"


async def test_expiry_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Second sweep finds nothing to do — once expired, always
    expired (until status is changed by a different code path)."""
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    async with session_factory() as db:
        driver, episode = await _seed_user_driver_episode(db)
        await _add_pres(
            db, driver_id=driver, episode_id=episode,
            status="proposed",
            valid_until=now - timedelta(days=1),
        )
        first = await expire_overdue_prescriptions(db, now=now)
        second = await expire_overdue_prescriptions(db, now=now)
    assert first == 1
    assert second == 0


def test_celery_beat_schedule_includes_nightly_expiry() -> None:
    """The 'runs nightly' half of T223: the beat schedule must
    register the expiry task at a daily cadence so the sweep
    actually fires in production."""
    from infra.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule
    assert "nightly-prescription-expiry" in schedule
    entry = schedule["nightly-prescription-expiry"]
    assert entry["task"] == "infra.celery_app.nightly_prescription_expiry"
