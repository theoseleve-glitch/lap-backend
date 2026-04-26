"""T220 — Confidence change propagates from outcome to episode.

When `persist_outcome` records a met or not_met verdict, the
linked WeaknessEpisode's `confidence` must move:

    met     → confidence -= 0.5  (weakness is being fixed)
    not_met → confidence += 0.1  (weakness persists despite drill)

Inconclusive outcomes don't write a row (T219), so they can't
propagate either.

Both ends are clamped to [0, 1] so downstream consumers can
trust the column is bounded. The outcome row's
`confidence_change` records the ACTUAL delta applied (after
clamping), not the raw nominal delta — so an audit can replay
state honestly.

Episodes that auto-decay via T209 are independent: T220's change
is direct evidence (we measured), T209's decay is the absence of
evidence. They compose; neither resets the other.
"""

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import (
    ActionOutcome,
    ActionPrescription,
    DriverProfile,
    User,
    WeaknessEpisode,
)
from infra.outcome_persistence import persist_outcome

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def session_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[async_sessionmaker[AsyncSession], None, None]:
    db_path = tmp_path / "conf.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    yield factory


_DEFAULT_CONDITION = {
    "metric": "apex_speed",
    "comparison": "gte",
    "target_pct": 0.96,
    "measurement_window": "next_3_sessions",
}


async def _seed(
    db: AsyncSession,
    *,
    episode_confidence: float,
    has_episode: bool = True,
) -> ActionPrescription:
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    episode_id = uuid6.uuid7() if has_episode else None
    prescription_id = uuid6.uuid7()
    now = datetime.now(UTC)
    db.add(
        User(
            id=user_id,
            clerk_user_id=f"u_cp_{user_id.hex}",
            email=f"cp_{user_id.hex}@test",
        )
    )
    db.add(
        DriverProfile(id=driver_id, user_id=user_id, display_name="A")
    )
    if has_episode:
        db.add(
            WeaknessEpisode(
                id=episode_id, driver_id=driver_id,
                category="technique", subcategory="late_braking",
                first_observed_at=now, last_observed_at=now,
                recurrence_count=1, sessions_affected=[],
                severity="medium", confidence=episode_confidence,
                status="open", linked_prescriptions=[],
                linked_insights=[],
                library_rule_id="br.late_braking.apex_speed_loss",
            )
        )
    db.add(
        ActionPrescription(
            id=prescription_id, driver_id=driver_id,
            weakness_episode_id=episode_id, created_at=now,
            priority="p1", title="t", rationale="r",
            expected_context={},
            success_condition=_DEFAULT_CONDITION,
            status="accepted", drill_template_id="drill.x",
        )
    )
    await db.commit()
    return (
        await db.execute(
            select(ActionPrescription).where(
                ActionPrescription.id == prescription_id
            )
        )
    ).scalar_one()


async def _read_episode_confidence(
    db: AsyncSession, episode_id: object
) -> float:
    return (
        await db.execute(
            select(WeaknessEpisode.confidence).where(
                WeaknessEpisode.id == episode_id
            )
        )
    ).scalar_one()


async def _read_outcome_change(
    db: AsyncSession, prescription_id: object
) -> float:
    return (
        await db.execute(
            select(ActionOutcome.confidence_change).where(
                ActionOutcome.prescription_id == prescription_id
            )
        )
    ).scalar_one()


async def test_met_outcome_drops_episode_confidence_by_half(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        pres = await _seed(db, episode_confidence=1.0)
        await persist_outcome(
            db, prescription=pres,
            measured_value=96.0, reference_value=100.0,
            session_ids_measured=[],
            measured_at=datetime.now(UTC),
        )
        new_conf = await _read_episode_confidence(
            db, pres.weakness_episode_id
        )
    assert new_conf == pytest.approx(0.5)


async def test_met_outcome_clamps_at_zero(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        pres = await _seed(db, episode_confidence=0.3)
        await persist_outcome(
            db, prescription=pres,
            measured_value=96.0, reference_value=100.0,
            session_ids_measured=[],
            measured_at=datetime.now(UTC),
        )
        new_conf = await _read_episode_confidence(
            db, pres.weakness_episode_id
        )
    assert new_conf == pytest.approx(0.0)


async def test_not_met_outcome_raises_episode_confidence_slightly(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        pres = await _seed(db, episode_confidence=0.6)
        await persist_outcome(
            db, prescription=pres,
            measured_value=95.0, reference_value=100.0,
            session_ids_measured=[],
            measured_at=datetime.now(UTC),
        )
        new_conf = await _read_episode_confidence(
            db, pres.weakness_episode_id
        )
    assert new_conf == pytest.approx(0.7)


async def test_not_met_outcome_clamps_at_one(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        pres = await _seed(db, episode_confidence=0.95)
        await persist_outcome(
            db, prescription=pres,
            measured_value=95.0, reference_value=100.0,
            session_ids_measured=[],
            measured_at=datetime.now(UTC),
        )
        new_conf = await _read_episode_confidence(
            db, pres.weakness_episode_id
        )
    assert new_conf == pytest.approx(1.0)


async def test_inconclusive_does_not_touch_episode_confidence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No outcome row, no propagation. The episode looks the same
    as if persist_outcome had never been called."""
    async with session_factory() as db:
        pres = await _seed(db, episode_confidence=0.7)
        await persist_outcome(
            db, prescription=pres,
            measured_value=10.0, reference_value=0.0,
            session_ids_measured=[],
            measured_at=datetime.now(UTC),
        )
        new_conf = await _read_episode_confidence(
            db, pres.weakness_episode_id
        )
    assert new_conf == pytest.approx(0.7)


async def test_outcome_records_actual_confidence_change_after_clamp(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """If the nominal -0.5 would push past 0, the row records the
    real -0.3 delta that was applied — so audit replay matches."""
    async with session_factory() as db:
        pres = await _seed(db, episode_confidence=0.3)
        await persist_outcome(
            db, prescription=pres,
            measured_value=96.0, reference_value=100.0,
            session_ids_measured=[],
            measured_at=datetime.now(UTC),
        )
        change = await _read_outcome_change(db, pres.id)
    assert change == pytest.approx(-0.3)


async def test_prescription_with_no_episode_does_not_crash(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ADR-005 allows weakness_episode_id=None for prescriptions
    born from standalone insights. Confidence_change must be 0
    in that case — there's nothing to propagate to."""
    async with session_factory() as db:
        pres = await _seed(
            db, episode_confidence=0.0, has_episode=False
        )
        outcome_id, verdict = await persist_outcome(
            db, prescription=pres,
            measured_value=96.0, reference_value=100.0,
            session_ids_measured=[],
            measured_at=datetime.now(UTC),
        )
        change = await _read_outcome_change(db, pres.id)
    assert outcome_id is not None
    assert verdict == "met"
    assert change == pytest.approx(0.0)
