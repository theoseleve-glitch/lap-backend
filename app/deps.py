import os
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

import jwt
import redis
import uuid6
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Config
from app.error_codes import ErrorCode
from domain.billing import is_in_grace_period
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.idempotency_cache import IdempotencyCache
from infra.job_event_bus import InMemoryJobEventBus, JobEventBusProtocol
from infra.orm import User
from infra.pairing_store import PairingStoreProtocol, RedisPairingStore
from infra.posthog import PosthogClientProtocol, make_posthog_client
from infra.storage import BlobStoreProtocol, LocalBlobStore, R2BlobStore
from infra.suggest_cache import SuggestCache
from infra.task_dispatcher import InMemoryTaskDispatcher, TaskDispatcherProtocol


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
def get_pairing_store() -> PairingStoreProtocol:
    client = redis.Redis.from_url(get_config().redis_url)
    return RedisPairingStore(client)


@lru_cache
def get_idempotency_cache() -> IdempotencyCache:
    client = redis.Redis.from_url(get_config().redis_url)
    return IdempotencyCache(client)


@lru_cache
def get_suggest_cache() -> SuggestCache:
    client = redis.Redis.from_url(get_config().redis_url)
    return SuggestCache(client)


@lru_cache
def get_blob_store() -> BlobStoreProtocol:
    cfg = get_config()
    if cfg.r2_bucket:
        return R2BlobStore(
            bucket=cfg.r2_bucket,
            endpoint_url=cfg.r2_endpoint_url or None,
            access_key_id=cfg.r2_access_key_id.get_secret_value(),
            secret_access_key=cfg.r2_secret_access_key.get_secret_value(),
        )
    return LocalBlobStore(Path(cfg.storage_root))


@lru_cache
def get_task_dispatcher() -> TaskDispatcherProtocol:
    # T103: in-memory placeholder; T107 swaps to a Celery-backed
    # dispatcher behind the same protocol.
    return InMemoryTaskDispatcher()


@lru_cache
def get_job_event_bus() -> JobEventBusProtocol:
    # T113: in-memory bus, single-process. A Redis-pub/sub-backed
    # implementation will satisfy the same protocol later so the SSE
    # endpoint stays unchanged.
    return InMemoryJobEventBus()


@lru_cache
def get_posthog_client() -> PosthogClientProtocol:
    # T169 — empty key in dev/CI returns a no-op stand-in; deploys
    # with POSTHOG_PROJECT_API_KEY get a real client. Reads from env
    # directly (not Config) so the dep stays callable even when other
    # secrets aren't set — important for narrow test fixtures that
    # don't seed Clerk/Stripe vars.
    return make_posthog_client(
        project_api_key=os.getenv("POSTHOG_PROJECT_API_KEY", ""),
        host=os.getenv("POSTHOG_HOST", "https://us.i.posthog.com"),
    )


_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail=ErrorCode.INVALID_TOKEN,
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
    posthog_client: Annotated[
        PosthogClientProtocol, Depends(get_posthog_client)
    ],
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
    # T174: identify the new user to PostHog. Only fires on the
    # auto-provision branch — repeat logins return early above and
    # don't re-write the person profile.
    posthog_client.set(
        distinct_id=str(user.id),
        properties={
            "email": user.email,
            "clerk_user_id": user.clerk_user_id,
        },
    )
    return user


_FORBIDDEN_NOT_ADMIN = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN, detail=ErrorCode.ADMIN_REQUIRED
)


async def require_admin(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    if not user.is_admin:
        raise _FORBIDDEN_NOT_ADMIN
    return user


# Stripe statuses that grant product access. `past_due` is intentionally
# absent — T163 layers a soft 48h grace period on top of this gate.
_ACTIVE_SUBSCRIPTION_STATUSES = frozenset({"active", "trialing"})

_PAYMENT_REQUIRED = HTTPException(
    status_code=status.HTTP_402_PAYMENT_REQUIRED,
    detail=ErrorCode.SUBSCRIPTION_REQUIRED,
)


async def require_active_subscription(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    if user.subscription_status in _ACTIVE_SUBSCRIPTION_STATUSES:
        return user
    if is_in_grace_period(
        user.subscription_status,
        user.trial_ended_at,
        datetime.now(UTC),
    ):
        return user
    raise _PAYMENT_REQUIRED
