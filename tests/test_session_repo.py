from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, User
from infra.session_repo import SessionRepo

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


async def test_create_persists_a_session(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            repo = SessionRepo(db)
            sess = await repo.create(
                driver_id=driver.id, manifest={"source": "iracing"}
            )
            assert sess.id is not None
            assert sess.driver_id == driver.id
            assert sess.manifest == {"source": "iracing"}
            assert sess.status == "uploaded"
            assert sess.created_at is not None
    finally:
        await engine.dispose()


async def test_get_returns_session_by_id(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            repo = SessionRepo(db)
            created = await repo.create(driver_id=driver.id, manifest={})
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
            repo = SessionRepo(db)
            assert await repo.get(uuid6.uuid7()) is None
    finally:
        await engine.dispose()


async def test_list_by_driver_returns_only_that_drivers_sessions(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            d1 = await _seed_driver(db)
            user2 = User(id=uuid6.uuid7(), clerk_user_id="u2", email="u2@test")
            d2 = DriverProfile(
                id=uuid6.uuid7(), user_id=user2.id, display_name="B"
            )
            db.add_all([user2, d2])
            await db.commit()

            repo = SessionRepo(db)
            await repo.create(driver_id=d1.id, manifest={"n": 1})
            await repo.create(driver_id=d1.id, manifest={"n": 2})
            await repo.create(driver_id=d2.id, manifest={"n": 3})

            d1_sessions = await repo.list_by_driver(d1.id)
            assert len(d1_sessions) == 2
            assert {s.manifest["n"] for s in d1_sessions} == {1, 2}

            d2_sessions = await repo.list_by_driver(d2.id)
            assert len(d2_sessions) == 1
            assert d2_sessions[0].manifest == {"n": 3}
    finally:
        await engine.dispose()


async def test_list_by_driver_empty_when_no_sessions(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            repo = SessionRepo(db)
            assert await repo.list_by_driver(driver.id) == []
    finally:
        await engine.dispose()


async def test_update_status_changes_session_status(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            repo = SessionRepo(db)
            created = await repo.create(driver_id=driver.id, manifest={})
            assert created.status == "uploaded"

            updated = await repo.update_status(created.id, "processing")
            assert updated is not None
            assert updated.status == "processing"

            reloaded = await repo.get(created.id)
            assert reloaded is not None
            assert reloaded.status == "processing"
    finally:
        await engine.dispose()


async def test_update_status_returns_none_for_missing_id(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            repo = SessionRepo(db)
            assert await repo.update_status(uuid6.uuid7(), "processing") is None
    finally:
        await engine.dispose()
