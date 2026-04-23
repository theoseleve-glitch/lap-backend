from functools import lru_cache
from typing import Never

from app.config import Config


@lru_cache
def get_config() -> Config:
    return Config()


async def get_db() -> Never:
    raise NotImplementedError


async def get_current_user() -> Never:
    raise NotImplementedError
