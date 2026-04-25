from collections.abc import AsyncGenerator
from functools import lru_cache
from typing import Annotated, Any

import jwt
import uuid6
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Config
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import User
from infra.pairing_store import PairingStore


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


@lru_cache
def get_pairing_store() -> PairingStore:
    return PairingStore()


_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="invalid_token",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_claims(
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


async def get_current_user(
    claims: Annotated[dict[str, Any], Depends(get_current_claims)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    sub = claims.get("sub")
    email = claims.get("email")
    if not sub or not email:
        raise _UNAUTHORIZED

    existing = (
        await db.execute(select(User).where(User.clerk_user_id == sub))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    user = User(id=uuid6.uuid7(), clerk_user_id=sub, email=email)
    db.add(user)
    await db.commit()
    return user


_FORBIDDEN_NOT_ADMIN = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN, detail="admin_required"
)


async def require_admin(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    if not user.is_admin:
        raise _FORBIDDEN_NOT_ADMIN
    return user
