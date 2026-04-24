from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from infra.db import make_engine, make_session_factory

TEST_URL = "sqlite+aiosqlite:///:memory:"


async def test_make_engine_roundtrips_select_one() -> None:
    engine = make_engine(TEST_URL)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as session:
            result = await session.execute(text("SELECT 1"))
            assert result.scalar() == 1
    finally:
        await engine.dispose()


async def test_session_factory_returns_async_session() -> None:
    engine = make_engine(TEST_URL)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as session:
            assert isinstance(session, AsyncSession)
    finally:
        await engine.dispose()
