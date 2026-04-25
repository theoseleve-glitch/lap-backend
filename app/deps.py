from collections.abc import AsyncGenerator
from functools import lru_cache
from typing import Never

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Config
from infra.db import make_engine, make_session_factory


@lru_cache
def get_config() -> Config:
    return Config()


@lru_cache
def _make_session_factory_for(url: str) -> async_sessionmaker[AsyncSession]:
    engine = make_engine(url)
    return make_session_factory(engine)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    factory = _make_session_factory_for(get_config().database_url)
    async with factory() as session:
        yield session


async def get_current_user() -> Never:
    raise NotImplementedError
