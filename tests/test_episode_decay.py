"""T209 — nightly WeaknessEpisode confidence decay.

Per ADR-005 §5.2, every WeaknessEpisode carries `confidence`
(0..1) that decays at `confidence_decay_per_day` per day without
re-observation. Re-observation (T208 merge) resets the clock by
advancing `last_observed_at`.

T209 is the daily job that recomputes the stored confidence from
the formula `1.0 - decay * (now - last_observed_at)` clamped to
[0, 1]. Idempotent: two runs same day produce the same final
state. A missed nightly tick is harmless — the next run catches
up automatically.
"""

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.episode_decay import (
    decay_confidence,
    decay_open_episodes,
)
from infra.orm import DriverProfile, User, WeaknessEpisode

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def session_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[async_sessionmaker[AsyncSession], None, None]:
    db_path = tmp_path / "decay.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    yield factory


# ── pure decay formula ────────────────────────────────────────


def test_decay_zero_days_returns_full_confidence() -> None:
    """No time elapsed = no decay. The formula handles this
    cleanly without special-casing."""
    assert decay_confidence(decay_per_day=0.02, days=0.0) == 1.0


def test_decay_one_day_at_default_rate() -> None:
    """Default decay 0.02/day → 1.0 - 0.02 = 0.98 after one day."""
    assert decay_confidence(decay_per_day=0.02, days=1.0) == pytest.approx(
        0.98
    )


def test_decay_is_linear_with_days() -> None:
    """Spec says linear decay — 5 days at 0.02 = 0.10 lost."""
    assert decay_confidence(decay_per_day=0.02, days=5.0) == pytest.approx(
        0.90
    )


def test_decay_clamps_at_zero() -> None:
    """Confidence can't go negative — once an episode hasn't
    been observed for confidence/decay days, it floors at 0."""
    assert (
        decay_confidence(decay_per_day=0.02, days=100.0) == 0.0
    )


def test_decay_respects_custom_rate() -> None:
    """Some weakness types decay faster — the per-row rate is
    what the formula multiplies by, no global constant."""
    assert decay_confidence(decay_per_day=0.10, days=3.0) == pytest.approx(
        0.70
    )


def test_decay_handles_fractional_days() -> None:
    """Half a day at 0.02 = 0.01 lost. Useful when the job runs
    at slightly different clock times."""
    assert decay_confidence(decay_per_day=0.02, days=0.5) == pytest.approx(
        0.99
    )


# ── job-level integration ─────────────────────────────────────


async def _seed_open_episode(
    db: AsyncSession,
    *,
    last_observed_at: datetime,
    confidence: float = 1.0,
    decay_per_day: float = 0.02,
    suffix: str = "",
) -> UUID:
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    episode_id = uuid6.uuid7()
    db.add(
        User(
            id=user_id,
            clerk_user_id=f"u_decay{suffix}_{user_id.hex[:8]}",
            email=f"decay{suffix}_{user_id.hex[:8]}@test",
        )
    )
    db.add(
        DriverProfile(id=driver_id, user_id=user_id, display_name="A")
    )
    db.add(
        WeaknessEpisode(
            id=episode_id,
            driver_id=driver_id,
            category="technique",
            subcategory="late_braking",
            first_observed_at=last_observed_at,
            last_observed_at=last_observed_at,
            recurrence_count=1,
            sessions_affected=[],
            severity="medium",
            confidence=confidence,
            confidence_decay_per_day=decay_per_day,
            status="open",
            linked_prescriptions=[],
            linked_insights=[],
            library_rule_id="br.late_braking.apex_speed_loss",
        )
    )
    await db.commit()
    return episode_id


async def test_decay_job_empty_db_returns_zero(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        n = await decay_open_episodes(db, now=datetime.now(UTC))
    assert n == 0


async def test_decay_job_recomputes_open_episode_confidence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Episode last observed 5 days ago → 5 * 0.02 = 0.10 lost,
    confidence falls to 0.90."""
    now = datetime(2026, 4, 26, tzinfo=UTC)
    last_observed = now - timedelta(days=5)
    async with session_factory() as db:
        ep_id = await _seed_open_episode(
            db, last_observed_at=last_observed
        )
        n = await decay_open_episodes(db, now=now)
        ep = (
            await db.execute(
                select(WeaknessEpisode).where(
                    WeaknessEpisode.id == ep_id
                )
            )
        ).scalar_one()
    assert n == 1
    assert ep.confidence == pytest.approx(0.90)


async def test_decay_job_skips_resolved_episodes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`status` filter only catches `open`. Resolved/dormant rows
    keep their historical confidence (whatever last set them)."""
    now = datetime(2026, 4, 26, tzinfo=UTC)
    last_observed = now - timedelta(days=5)
    async with session_factory() as db:
        # Manually insert a resolved episode with confidence 0.5.
        ep_id = await _seed_open_episode(
            db, last_observed_at=last_observed,
            confidence=0.5, suffix="_res",
        )
        ep = (
            await db.execute(
                select(WeaknessEpisode).where(
                    WeaknessEpisode.id == ep_id
                )
            )
        ).scalar_one()
        ep.status = "resolved"
        ep.resolved_at = now
        await db.commit()
        n = await decay_open_episodes(db, now=now)
        ep_after = (
            await db.execute(
                select(WeaknessEpisode).where(
                    WeaknessEpisode.id == ep_id
                )
            )
        ).scalar_one()
    assert n == 0
    assert ep_after.confidence == 0.5  # untouched


async def test_decay_job_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two runs same `now` → same final confidence. Recomputing
    from last_observed_at means the stored value can be set,
    overwritten, and set again to the same number."""
    now = datetime(2026, 4, 26, tzinfo=UTC)
    last_observed = now - timedelta(days=3)
    async with session_factory() as db:
        ep_id = await _seed_open_episode(
            db, last_observed_at=last_observed
        )
        await decay_open_episodes(db, now=now)
        first = (
            await db.execute(
                select(WeaknessEpisode).where(
                    WeaknessEpisode.id == ep_id
                )
            )
        ).scalar_one()
        first_conf = first.confidence
        # Run again — second pass detects no change, returns 0.
        n_second = await decay_open_episodes(db, now=now)
        second = (
            await db.execute(
                select(WeaknessEpisode).where(
                    WeaknessEpisode.id == ep_id
                )
            )
        ).scalar_one()
    assert n_second == 0
    assert second.confidence == first_conf


async def test_decay_job_clamps_at_zero(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An episode last observed 100 days ago at 0.02/day would be
    -1.0 without clamping. Floor at 0.0."""
    now = datetime(2026, 4, 26, tzinfo=UTC)
    last_observed = now - timedelta(days=100)
    async with session_factory() as db:
        ep_id = await _seed_open_episode(
            db, last_observed_at=last_observed
        )
        await decay_open_episodes(db, now=now)
        ep = (
            await db.execute(
                select(WeaknessEpisode).where(
                    WeaknessEpisode.id == ep_id
                )
            )
        ).scalar_one()
    assert ep.confidence == 0.0


async def test_decay_job_processes_multiple_episodes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """N open episodes → N decayed in one pass. The return value
    is the count of rows whose stored confidence actually moved
    (so the operator can spot a no-op run vs a real one)."""
    now = datetime(2026, 4, 26, tzinfo=UTC)
    async with session_factory() as db:
        await _seed_open_episode(
            db, last_observed_at=now - timedelta(days=5),
            suffix="_a",
        )
        await _seed_open_episode(
            db, last_observed_at=now - timedelta(days=10),
            suffix="_b",
        )
        n = await decay_open_episodes(db, now=now)
    assert n == 2


async def test_decay_job_preserves_last_observed_at(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The job ONLY mutates confidence. last_observed_at is the
    source of truth for the formula — touching it would corrupt
    the decay clock."""
    now = datetime(2026, 4, 26, tzinfo=UTC)
    last_observed = now - timedelta(days=7)
    async with session_factory() as db:
        ep_id = await _seed_open_episode(
            db, last_observed_at=last_observed
        )
        await decay_open_episodes(db, now=now)
        ep = (
            await db.execute(
                select(WeaknessEpisode).where(
                    WeaknessEpisode.id == ep_id
                )
            )
        ).scalar_one()
    assert ep.last_observed_at.replace(tzinfo=UTC) == last_observed
