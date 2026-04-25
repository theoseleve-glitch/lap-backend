from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.driver_repo import DriverRepo
from infra.orm import User

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def upgraded_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


async def _seed_user(db) -> User:  # type: ignore[no-untyped-def]
    user = User(id=uuid6.uuid7(), clerk_user_id="u", email="u@test")
    db.add(user)
    await db.commit()
    return user


async def test_create_persists_a_driver_with_defaults(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            user = await _seed_user(db)
            repo = DriverRepo(db)
            driver = await repo.create(user_id=user.id, display_name="Theo")
            assert driver.id is not None
            assert driver.user_id == user.id
            assert driver.display_name == "Theo"
            assert driver.skill_level == "beginner"
            assert driver.cohort_opt_in is False
    finally:
        await engine.dispose()


async def test_get_returns_driver_by_id(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            user = await _seed_user(db)
            repo = DriverRepo(db)
            created = await repo.create(user_id=user.id, display_name="X")
            loaded = await repo.get(created.id)
            assert loaded is not None
            assert loaded.id == created.id
    finally:
        await engine.dispose()


async def test_get_returns_none_for_missing_id(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            repo = DriverRepo(db)
            assert await repo.get(uuid6.uuid7()) is None
    finally:
        await engine.dispose()


async def test_update_settings_changes_only_provided_fields(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            user = await _seed_user(db)
            repo = DriverRepo(db)
            d = await repo.create(user_id=user.id, display_name="Old")
            assert d.skill_level == "beginner"
            assert d.cohort_opt_in is False

            updated = await repo.update_settings(d.id, display_name="New")
            assert updated is not None
            assert updated.display_name == "New"
            assert updated.skill_level == "beginner"
            assert updated.cohort_opt_in is False

            updated2 = await repo.update_settings(
                d.id, skill_level="intermediate", cohort_opt_in=True
            )
            assert updated2 is not None
            assert updated2.display_name == "New"
            assert updated2.skill_level == "intermediate"
            assert updated2.cohort_opt_in is True
    finally:
        await engine.dispose()


async def test_update_settings_returns_none_for_missing_id(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            repo = DriverRepo(db)
            result = await repo.update_settings(uuid6.uuid7(), display_name="x")
            assert result is None
    finally:
        await engine.dispose()
