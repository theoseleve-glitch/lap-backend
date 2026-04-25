"""T161 — `POST /api/v1/admin/billing/sync/{user_id}` force-syncs
the target user's `subscription_status` from Stripe.

Admin-only. Useful when a webhook was lost and the daily reconcile
hasn't run yet — operator asks "what does Stripe say *right now*"
and the DB catches up.
"""

import base64
import time
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import jwt
import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alembic import command
from app.config import Config as AppConfig
from app.deps import get_config, get_db, get_jwks_client
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import User

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _build_keypair(kid: str = "test-kid") -> dict[str, Any]:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pn = priv.public_key().public_numbers()
    n = pn.n.to_bytes((pn.n.bit_length() + 7) // 8, "big")
    e = pn.e.to_bytes((pn.e.bit_length() + 7) // 8, "big")
    jwk = {
        "kty": "RSA", "kid": kid, "use": "sig", "alg": "RS256",
        "n": _b64u(n), "e": _b64u(e),
    }
    return {"priv_pem": priv_pem, "kid": kid, "jwks": {"keys": [jwk]}}


def _bearer(keys: dict[str, Any], sub: str, email: str) -> str:
    return jwt.encode(
        {"sub": sub, "email": email, "exp": int(time.time()) + 3600},
        keys["priv_pem"],
        algorithm="RS256",
        headers={"kid": keys["kid"]},
    )


@pytest.fixture
def configured_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[dict[str, Any], None, None]:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("REDIS_URL", "redis://x")
    monkeypatch.setenv("CLERK_SECRET", "sk_test_clerk")
    monkeypatch.setenv("STRIPE_SECRET", "sk_test_stripe")
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")

    keys = _build_keypair()
    engine = make_engine(url)
    factory = make_session_factory(engine)

    async def fake_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    def fake_jwks_client() -> ClerkJwksClient:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=keys["jwks"])

        return ClerkJwksClient(
            "https://test.clerk", transport=httpx.MockTransport(handler)
        )

    def fake_config() -> AppConfig:
        return AppConfig()

    app.dependency_overrides[get_db] = fake_get_db
    app.dependency_overrides[get_jwks_client] = fake_jwks_client
    app.dependency_overrides[get_config] = fake_config
    try:
        yield {"keys": keys, "factory": factory}
    finally:
        app.dependency_overrides.clear()


async def _seed_user(
    factory: Any,
    sub: str,
    email: str,
    *,
    is_admin: bool = False,
    stripe_customer_id: str | None = None,
    subscription_status: str = "inactive",
) -> User:
    async with factory() as session:
        user = User(
            id=uuid6.uuid7(),
            clerk_user_id=sub,
            email=email,
            is_admin=is_admin,
            stripe_customer_id=stripe_customer_id,
            subscription_status=subscription_status,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


def _patched_iter(subs: list[dict[str, Any]]) -> Any:
    class FakeListing:
        def __init__(self, items: list[dict[str, Any]]) -> None:
            self._items = items

        def auto_paging_iter(self) -> Any:
            return iter(self._items)

    return FakeListing(subs)


async def test_sync_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    client = TestClient(app)
    resp = client.post(f"/api/v1/admin/billing/sync/{uuid6.uuid7()}")
    assert resp.status_code == 401


async def test_sync_non_admin_returns_403(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    target = await _seed_user(
        factory, "user_target", "tgt@test", subscription_status="trialing"
    )
    await _seed_user(
        factory, "user_caller", "caller@test", is_admin=False
    )
    token = _bearer(keys, "user_caller", "caller@test")
    client = TestClient(app)
    resp = client.post(
        f"/api/v1/admin/billing/sync/{target.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_sync_target_user_not_found_returns_404(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_user(
        factory, "user_admin_404", "admin404@test", is_admin=True
    )
    token = _bearer(keys, "user_admin_404", "admin404@test")
    client = TestClient(app)
    resp = client.post(
        f"/api/v1/admin/billing/sync/{uuid6.uuid7()}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


async def test_sync_target_without_customer_id_returns_409(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    target = await _seed_user(
        factory,
        "user_target_nc",
        "nc@test",
        stripe_customer_id=None,
        subscription_status="inactive",
    )
    await _seed_user(
        factory, "user_admin_409", "admin409@test", is_admin=True
    )
    token = _bearer(keys, "user_admin_409", "admin409@test")
    client = TestClient(app)
    resp = client.post(
        f"/api/v1/admin/billing/sync/{target.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409


async def test_sync_writes_stripe_status_into_db(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    target = await _seed_user(
        factory,
        "user_target_sync",
        "sync@test",
        stripe_customer_id="cus_admin_sync",
        subscription_status="trialing",  # stale
    )
    await _seed_user(
        factory, "user_admin_sync", "admin_sync@test", is_admin=True
    )
    token = _bearer(keys, "user_admin_sync", "admin_sync@test")

    fake_listing = _patched_iter(
        [{"id": "sub_x", "status": "active", "customer": "cus_admin_sync"}]
    )
    with patch("stripe.Subscription.list", return_value=fake_listing):
        client = TestClient(app)
        resp = client.post(
            f"/api/v1/admin/billing/sync/{target.id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["subscription_status"] == "active"
    async with factory() as session:
        row = (
            await session.execute(select(User).where(User.id == target.id))
        ).scalar_one()
        assert row.subscription_status == "active"


async def test_sync_target_with_no_stripe_subs_keeps_status(
    configured_app: dict[str, Any],
) -> None:
    # Stripe knows the customer but they have no subs yet — leave
    # status untouched; admin sync isn't a "set to default" tool.
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    target = await _seed_user(
        factory,
        "user_target_empty",
        "empty@test",
        stripe_customer_id="cus_no_subs",
        subscription_status="trialing",
    )
    await _seed_user(
        factory, "user_admin_empty", "admin_empty@test", is_admin=True
    )
    token = _bearer(keys, "user_admin_empty", "admin_empty@test")

    fake_listing = _patched_iter([])
    with patch("stripe.Subscription.list", return_value=fake_listing):
        client = TestClient(app)
        resp = client.post(
            f"/api/v1/admin/billing/sync/{target.id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["subscription_status"] == "trialing"  # unchanged
