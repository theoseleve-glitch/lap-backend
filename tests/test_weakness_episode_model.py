from datetime import UTC, datetime
from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, User, WeaknessEpisode

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def upgraded_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


async def _seed_driver(db) -> DriverProfile:  # type: ignore[no-untyped-def]
    user = User(id=uuid6.uuid7(), clerk_user_id="u", email="u@test")
    driver = DriverProfile(id=uuid6.uuid7(), user_id=user.id, display_name="A")
    db.add_all([user, driver])
    await db.commit()
    return driver


async def test_weakness_episode_roundtrips_all_fields(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            now = datetime(2026, 4, 25, 10, 0, tzinfo=UTC)
            episode = WeaknessEpisode(
                id=uuid6.uuid7(),
                driver_id=driver.id,
                category="technique",
                subcategory="trail_braking",
                corner_tag="T3_eau_rouge",
                environment_scope=["sim_practice", "sim_race"],
                first_observed_at=now,
                last_observed_at=now,
                recurrence_count=4,
                sessions_affected=[str(uuid6.uuid7()), str(uuid6.uuid7())],
                severity="medium",
                confidence=0.75,
                confidence_decay_per_day=0.05,
                status="open",
                resolved_at=None,
                linked_prescriptions=[str(uuid6.uuid7())],
                linked_insights=[str(uuid6.uuid7())],
                library_rule_id="rule.technique.trail_braking.v1",
            )
            db.add(episode)
            await db.commit()

            loaded = (await db.execute(select(WeaknessEpisode))).scalar_one()
            assert loaded.driver_id == driver.id
            assert loaded.category == "technique"
            assert loaded.subcategory == "trail_braking"
            assert loaded.corner_tag == "T3_eau_rouge"
            assert loaded.environment_scope == ["sim_practice", "sim_race"]
            assert loaded.first_observed_at == now
            assert loaded.last_observed_at == now
            assert loaded.recurrence_count == 4
            assert len(loaded.sessions_affected) == 2
            assert loaded.severity == "medium"
            assert loaded.confidence == 0.75
            assert loaded.confidence_decay_per_day == 0.05
            assert loaded.status == "open"
            assert loaded.resolved_at is None
            assert len(loaded.linked_prescriptions) == 1
            assert len(loaded.linked_insights) == 1
            assert loaded.library_rule_id == "rule.technique.trail_braking.v1"
    finally:
        await engine.dispose()


async def test_weakness_episode_applies_pydantic_defaults(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            now = datetime(2026, 4, 25, 10, 0, tzinfo=UTC)
            minimal = WeaknessEpisode(
                id=uuid6.uuid7(),
                driver_id=driver.id,
                category="consistency",
                subcategory="braking",
                first_observed_at=now,
                last_observed_at=now,
                recurrence_count=0,
                severity="low",
                confidence=0.5,
                status="open",
                library_rule_id="rule.consistency.braking.v1",
            )
            db.add(minimal)
            await db.commit()

            loaded = (await db.execute(select(WeaknessEpisode))).scalar_one()
            assert loaded.corner_tag is None
            assert loaded.environment_scope == []
            assert loaded.sessions_affected == []
            assert loaded.linked_prescriptions == []
            assert loaded.linked_insights == []
            assert loaded.confidence_decay_per_day == 0.02
            assert loaded.resolved_at is None
    finally:
        await engine.dispose()


async def test_resolved_episode_stores_resolved_at(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            first = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
            last = datetime(2026, 4, 20, 10, 0, tzinfo=UTC)
            resolved = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
            episode = WeaknessEpisode(
                id=uuid6.uuid7(),
                driver_id=driver.id,
                category="technique",
                subcategory="apex_hit",
                first_observed_at=first,
                last_observed_at=last,
                recurrence_count=7,
                severity="high",
                confidence=0.9,
                status="resolved",
                resolved_at=resolved,
                library_rule_id="rule.technique.apex.v1",
            )
            db.add(episode)
            await db.commit()
            loaded = (await db.execute(select(WeaknessEpisode))).scalar_one()
            assert loaded.status == "resolved"
            assert loaded.resolved_at == resolved
    finally:
        await engine.dispose()
