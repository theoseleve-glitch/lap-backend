from datetime import UTC, datetime
from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, User
from infra.weakness_repo import WeaknessRepo

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


async def test_open_creates_episode_in_open_state(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            now = datetime(2026, 4, 25, 10, 0, tzinfo=UTC)
            repo = WeaknessRepo(db)
            episode = await repo.open(
                driver_id=driver.id,
                category="technique",
                subcategory="trail_braking",
                severity="medium",
                confidence=0.7,
                library_rule_id="rule.tech.trail.v1",
                first_observed_at=now,
                last_observed_at=now,
                recurrence_count=2,
            )
            assert episode.id is not None
            assert episode.driver_id == driver.id
            assert episode.status == "open"
            assert episode.resolved_at is None
            assert episode.confidence == 0.7
            assert episode.recurrence_count == 2
    finally:
        await engine.dispose()


async def test_update_confidence_changes_only_confidence(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            now = datetime(2026, 4, 25, tzinfo=UTC)
            repo = WeaknessRepo(db)
            ep = await repo.open(
                driver_id=driver.id,
                category="technique",
                subcategory="apex",
                severity="low",
                confidence=0.4,
                library_rule_id="rule.x",
                first_observed_at=now,
                last_observed_at=now,
                recurrence_count=1,
            )
            updated = await repo.update_confidence(ep.id, 0.85)
            assert updated is not None
            assert updated.confidence == 0.85
            assert updated.status == "open"
            assert updated.severity == "low"
    finally:
        await engine.dispose()


async def test_update_confidence_returns_none_for_missing_id(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            repo = WeaknessRepo(db)
            assert await repo.update_confidence(uuid6.uuid7(), 0.5) is None
    finally:
        await engine.dispose()


async def test_resolve_sets_status_and_resolved_at(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            opened_at = datetime(2026, 3, 1, tzinfo=UTC)
            resolved_at = datetime(2026, 4, 25, tzinfo=UTC)
            repo = WeaknessRepo(db)
            ep = await repo.open(
                driver_id=driver.id,
                category="consistency",
                subcategory="late_apex",
                severity="high",
                confidence=0.9,
                library_rule_id="rule.cons.apex.v1",
                first_observed_at=opened_at,
                last_observed_at=opened_at,
                recurrence_count=5,
            )
            updated = await repo.resolve(ep.id, resolved_at=resolved_at)
            assert updated is not None
            assert updated.status == "resolved"
            assert updated.resolved_at == resolved_at
    finally:
        await engine.dispose()


async def test_resolve_returns_none_for_missing_id(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            repo = WeaknessRepo(db)
            result = await repo.resolve(
                uuid6.uuid7(), resolved_at=datetime(2026, 4, 25, tzinfo=UTC)
            )
            assert result is None
    finally:
        await engine.dispose()
