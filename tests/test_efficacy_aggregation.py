"""T221 — Efficacy score aggregation across three grains.

Three pure-compute queries over (action_outcomes,
action_prescriptions, weakness_episodes):

    efficacy_for_prescription(prescription_id) → float | None
    efficacy_for_driver(driver_id)              → EfficacyStat
    efficacy_for_rule(library_rule_id)          → EfficacyStat

Per-prescription returns the single row's efficacy (or None if
no outcome exists yet — the prescription is open).

Per-driver averages efficacy across all the driver's outcomes —
the personal "how reliably do I act on prescriptions" stat.

Per-rule averages cross-driver across all outcomes whose linked
weakness used that library rule — the meta-signal "is this rule
actually a good prescription template, regardless of who runs
it." A low per-rule mean across many drivers is evidence to
revisit the rule itself, not the drivers.

EfficacyStat carries sample_size so consumers can decide whether
to trust the mean. T222 surfaces these via HTTP for the driver-
facing dashboard.
"""

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.efficacy_aggregation import (
    EfficacyStat,
    efficacy_for_driver,
    efficacy_for_prescription,
    efficacy_for_rule,
)
from infra.orm import (
    ActionOutcome,
    ActionPrescription,
    DriverProfile,
    User,
    WeaknessEpisode,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def session_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[async_sessionmaker[AsyncSession], None, None]:
    db_path = tmp_path / "eff.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    yield factory


async def _seed_user_driver(
    db: AsyncSession, *, slot: str
) -> UUID:
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    db.add(
        User(
            id=user_id,
            clerk_user_id=f"u_eff_{slot}_{user_id.hex}",
            email=f"eff_{slot}_{user_id.hex}@test",
        )
    )
    db.add(
        DriverProfile(id=driver_id, user_id=user_id, display_name=slot)
    )
    await db.commit()
    return driver_id


async def _seed_episode(
    db: AsyncSession, *, driver_id: UUID, library_rule_id: str
) -> UUID:
    episode_id = uuid6.uuid7()
    now = datetime.now(UTC)
    db.add(
        WeaknessEpisode(
            id=episode_id, driver_id=driver_id,
            category="technique", subcategory="late_braking",
            first_observed_at=now, last_observed_at=now,
            recurrence_count=1, sessions_affected=[],
            severity="medium", confidence=1.0, status="open",
            linked_prescriptions=[], linked_insights=[],
            library_rule_id=library_rule_id,
        )
    )
    await db.commit()
    return episode_id


async def _seed_prescription_with_outcome(
    db: AsyncSession,
    *,
    driver_id: UUID,
    episode_id: UUID,
    efficacy: float,
) -> UUID:
    """Create a prescription + a paired outcome row with the given
    efficacy score. Returns the prescription_id."""
    p_id = uuid6.uuid7()
    o_id = uuid6.uuid7()
    now = datetime.now(UTC)
    db.add(
        ActionPrescription(
            id=p_id, driver_id=driver_id,
            weakness_episode_id=episode_id, created_at=now,
            priority="p1", title="t", rationale="r",
            expected_context={}, success_condition={},
            status="completed", drill_template_id="drill.x",
            outcome_id=o_id,
        )
    )
    db.add(
        ActionOutcome(
            id=o_id, prescription_id=p_id, measured_at=now,
            session_ids_measured=[],
            success_condition_met=(efficacy >= 0.5),
            measured_value=0.0, delta_vs_baseline=0.0,
            confidence_change=0.0,
            prescription_efficacy_score=efficacy,
        )
    )
    await db.commit()
    return p_id


async def _seed_open_prescription_no_outcome(
    db: AsyncSession,
    *,
    driver_id: UUID,
    episode_id: UUID,
) -> UUID:
    p_id = uuid6.uuid7()
    now = datetime.now(UTC)
    db.add(
        ActionPrescription(
            id=p_id, driver_id=driver_id,
            weakness_episode_id=episode_id, created_at=now,
            priority="p1", title="t", rationale="r",
            expected_context={}, success_condition={},
            status="proposed", drill_template_id="drill.x",
        )
    )
    await db.commit()
    return p_id


# ------------------- per-prescription -------------------

async def test_efficacy_for_prescription_returns_outcome_score(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        driver = await _seed_user_driver(db, slot="A")
        episode = await _seed_episode(
            db, driver_id=driver, library_rule_id="rule.alpha"
        )
        p_id = await _seed_prescription_with_outcome(
            db, driver_id=driver, episode_id=episode, efficacy=1.0
        )
        score = await efficacy_for_prescription(db, p_id)
    assert score == pytest.approx(1.0)


async def test_efficacy_for_prescription_none_when_no_outcome(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        driver = await _seed_user_driver(db, slot="B")
        episode = await _seed_episode(
            db, driver_id=driver, library_rule_id="rule.alpha"
        )
        p_id = await _seed_open_prescription_no_outcome(
            db, driver_id=driver, episode_id=episode
        )
        score = await efficacy_for_prescription(db, p_id)
    assert score is None


# ---------------------- per-driver ----------------------

async def test_efficacy_for_driver_averages_outcomes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        driver = await _seed_user_driver(db, slot="C")
        episode = await _seed_episode(
            db, driver_id=driver, library_rule_id="rule.alpha"
        )
        await _seed_prescription_with_outcome(
            db, driver_id=driver, episode_id=episode, efficacy=1.0
        )
        await _seed_prescription_with_outcome(
            db, driver_id=driver, episode_id=episode, efficacy=0.0
        )
        await _seed_prescription_with_outcome(
            db, driver_id=driver, episode_id=episode, efficacy=0.5
        )
        stat = await efficacy_for_driver(db, driver)
    assert stat == EfficacyStat(
        sample_size=3, mean_efficacy=pytest.approx(0.5),
    )


async def test_efficacy_for_driver_zero_sample_when_empty(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        driver = await _seed_user_driver(db, slot="D")
        stat = await efficacy_for_driver(db, driver)
    assert stat.sample_size == 0
    assert stat.mean_efficacy is None


async def test_efficacy_for_driver_excludes_other_drivers_outcomes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Per-driver must NOT mix in another driver's outcomes."""
    async with session_factory() as db:
        driver_a = await _seed_user_driver(db, slot="E")
        driver_b = await _seed_user_driver(db, slot="F")
        ep_a = await _seed_episode(
            db, driver_id=driver_a, library_rule_id="rule.alpha"
        )
        ep_b = await _seed_episode(
            db, driver_id=driver_b, library_rule_id="rule.alpha"
        )
        await _seed_prescription_with_outcome(
            db, driver_id=driver_a, episode_id=ep_a, efficacy=1.0
        )
        await _seed_prescription_with_outcome(
            db, driver_id=driver_b, episode_id=ep_b, efficacy=0.0
        )
        stat = await efficacy_for_driver(db, driver_a)
    assert stat.sample_size == 1
    assert stat.mean_efficacy == pytest.approx(1.0)


# ----------------------- per-rule -----------------------

async def test_efficacy_for_rule_aggregates_across_drivers(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Per-rule is the meta-signal across the whole user base."""
    async with session_factory() as db:
        d1 = await _seed_user_driver(db, slot="G")
        d2 = await _seed_user_driver(db, slot="H")
        ep1 = await _seed_episode(
            db, driver_id=d1, library_rule_id="rule.shared"
        )
        ep2 = await _seed_episode(
            db, driver_id=d2, library_rule_id="rule.shared"
        )
        await _seed_prescription_with_outcome(
            db, driver_id=d1, episode_id=ep1, efficacy=1.0
        )
        await _seed_prescription_with_outcome(
            db, driver_id=d2, episode_id=ep2, efficacy=0.0
        )
        stat = await efficacy_for_rule(db, "rule.shared")
    assert stat.sample_size == 2
    assert stat.mean_efficacy == pytest.approx(0.5)


async def test_efficacy_for_rule_excludes_other_rules(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        driver = await _seed_user_driver(db, slot="I")
        ep_target = await _seed_episode(
            db, driver_id=driver, library_rule_id="rule.target"
        )
        ep_other = await _seed_episode(
            db, driver_id=driver, library_rule_id="rule.other"
        )
        await _seed_prescription_with_outcome(
            db, driver_id=driver, episode_id=ep_target, efficacy=1.0
        )
        await _seed_prescription_with_outcome(
            db, driver_id=driver, episode_id=ep_other, efficacy=0.0
        )
        stat = await efficacy_for_rule(db, "rule.target")
    assert stat.sample_size == 1
    assert stat.mean_efficacy == pytest.approx(1.0)


async def test_efficacy_for_rule_zero_sample_when_unknown(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        stat = await efficacy_for_rule(db, "rule.does_not_exist")
    assert stat.sample_size == 0
    assert stat.mean_efficacy is None
