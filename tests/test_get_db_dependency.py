from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from alembic import command
from app.deps import get_config, get_db
from infra.orm import User

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def configured_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("CLERK_SECRET", "sk_test_x")
    monkeypatch.setenv("STRIPE_SECRET", "sk_test_y")
    get_config.cache_clear()
    from app.deps import _make_session_factory_for

    _make_session_factory_for.cache_clear()
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


@pytest.mark.usefixtures("configured_db")
async def test_get_db_yields_an_async_session() -> None:
    agen = get_db()
    session = await anext(agen)
    try:
        assert isinstance(session, AsyncSession)
    finally:
        await agen.aclose()


@pytest.mark.usefixtures("configured_db")
async def test_get_db_session_can_execute_a_simple_query() -> None:
    agen = get_db()
    session = await anext(agen)
    try:
        result = await session.execute(text("SELECT 1"))
        assert result.scalar() == 1
    finally:
        await agen.aclose()


@pytest.mark.usefixtures("configured_db")
async def test_get_db_session_can_persist_orm_rows() -> None:
    agen = get_db()
    session = await anext(agen)
    try:
        user = User(
            id=uuid6.uuid7(),
            clerk_user_id="getdb-clerk",
            email="getdb@test",
        )
        session.add(user)
        await session.commit()

        loaded = (await session.execute(select(User))).scalar_one()
        assert loaded.email == "getdb@test"
    finally:
        await agen.aclose()


@pytest.mark.usefixtures("configured_db")
async def test_get_db_yields_distinct_sessions_per_call() -> None:
    agen1 = get_db()
    agen2 = get_db()
    s1 = await anext(agen1)
    s2 = await anext(agen2)
    try:
        assert s1 is not s2
    finally:
        await agen1.aclose()
        await agen2.aclose()
