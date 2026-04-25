from collections.abc import AsyncGenerator
from functools import lru_cache
from typing import Annotated, Any

import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Config
from infra.clerk import ClerkJwksClient
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


@lru_cache
def get_jwks_client() -> ClerkJwksClient:
    return ClerkJwksClient(get_config().clerk_issuer)


_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="invalid_token",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
    jwks_client: Annotated[
        ClerkJwksClient, Depends(get_jwks_client)
    ] = ...,  # type: ignore[assignment]
) -> dict[str, Any]:
    if authorization is None:
        raise _UNAUTHORIZED
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise _UNAUTHORIZED
    token = parts[1].strip()

    try:
        unverified = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise _UNAUTHORIZED from exc
    kid = unverified.get("kid")
    if not kid:
        raise _UNAUTHORIZED

    jwks = await jwks_client.fetch_jwks()
    key_dict = next(
        (k for k in jwks.get("keys", []) if k.get("kid") == kid), None
    )
    if key_dict is None:
        raise _UNAUTHORIZED

    try:
        public_key = jwt.PyJWK(key_dict).key
        claims: dict[str, Any] = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            options={"verify_aud": False},
        )
    except jwt.InvalidTokenError as exc:
        raise _UNAUTHORIZED from exc
    return claims
