from datetime import datetime
from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import User

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def upgraded_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    # Sync fixture: alembic.command.upgrade() calls asyncio.run() under the
    # async env template, so it cannot be awaited from inside an async test.
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


async def test_user_row_roundtrips_after_migration(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as session:
            u = User(
                id=uuid6.uuid7(),
                clerk_user_id="user_test_123",
                email="a@b.test",
            )
            session.add(u)
            await session.commit()

            result = await session.execute(select(User))
            loaded = result.scalar_one()
            assert loaded.clerk_user_id == "user_test_123"
            assert loaded.email == "a@b.test"
            assert loaded.is_admin is False
            assert loaded.subscription_status == "inactive"
            assert isinstance(loaded.created_at, datetime)
    finally:
        await engine.dispose()


async def test_user_clerk_user_id_is_unique(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as session:
            session.add(
                User(id=uuid6.uuid7(), clerk_user_id="dup", email="x@x.test"),
            )
            await session.commit()
        async with session_factory() as session:
            session.add(
                User(id=uuid6.uuid7(), clerk_user_id="dup", email="y@y.test"),
            )
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()
