from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, User

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def upgraded_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


async def test_driver_profile_round_trips_defaults(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        user_id = uuid6.uuid7()
        async with session_factory() as session:
            session.add(
                User(id=user_id, clerk_user_id="u1", email="u1@test"),
            )
            session.add(
                DriverProfile(
                    id=uuid6.uuid7(),
                    user_id=user_id,
                    display_name="Alex",
                ),
            )
            await session.commit()

            result = await session.execute(select(DriverProfile))
            loaded = result.scalar_one()
            assert loaded.display_name == "Alex"
            assert loaded.skill_level == "beginner"
            assert loaded.cohort_opt_in is False
    finally:
        await engine.dispose()


async def test_driver_profile_user_id_is_unique(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        user_id = uuid6.uuid7()
        async with session_factory() as session:
            session.add(User(id=user_id, clerk_user_id="u2", email="u2@test"))
            session.add(
                DriverProfile(
                    id=uuid6.uuid7(),
                    user_id=user_id,
                    display_name="Bobby",
                ),
            )
            await session.commit()
        async with session_factory() as session:
            session.add(
                DriverProfile(
                    id=uuid6.uuid7(),
                    user_id=user_id,  # duplicate — one profile per user
                    display_name="Bobby Second",
                ),
            )
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()
