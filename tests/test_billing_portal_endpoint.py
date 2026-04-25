"""T157 — `GET /api/v1/billing/portal` returns a Stripe Billing
Portal URL the client redirects to so the user can update payment
methods, cancel, or change plan.

Authenticated. Requires the user to have a `stripe_customer_id`
(populated when their first subscription was created in T152).
Stripe API call mocked.
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
    monkeypatch.setenv("APP_BASE_URL", "https://app.lap-coach.test")
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
    stripe_customer_id: str | None = None,
) -> User:
    async with factory() as session:
        user = User(
            id=uuid6.uuid7(),
            clerk_user_id=sub,
            email=email,
            stripe_customer_id=stripe_customer_id,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


def _fake_portal_session(
    url: str = "https://billing.stripe.test/session/abc",
) -> Any:
    class FakePortalSession:
        def __init__(self) -> None:
            self.id = "bps_test_1"
            self.url = url

    return FakePortalSession()


async def test_portal_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    client = TestClient(app)
    resp = client.get("/api/v1/billing/portal")
    assert resp.status_code == 401


async def test_portal_returns_url_for_user_with_customer_id(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_user(
        factory, "user_p_1", "p1@test", stripe_customer_id="cus_test_1"
    )
    token = _bearer(keys, "user_p_1", "p1@test")

    with patch(
        "stripe.billing_portal.Session.create",
        return_value=_fake_portal_session(
            "https://billing.stripe.test/portal/abc"
        ),
    ) as create_mock:
        client = TestClient(app)
        resp = client.get(
            "/api/v1/billing/portal",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["url"] == "https://billing.stripe.test/portal/abc"
    create_mock.assert_called_once()
    kwargs = create_mock.call_args.kwargs
    assert kwargs["customer"] == "cus_test_1"
    # return_url should land back in the app, not at Stripe.
    assert kwargs["return_url"].startswith("https://app.lap-coach.test")


async def test_portal_returns_409_when_user_has_no_customer_id(
    configured_app: dict[str, Any],
) -> None:
    # User signed up but never subscribed → no Stripe customer yet.
    # Frontend should route them to checkout instead.
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_user(
        factory, "user_p_2", "p2@test", stripe_customer_id=None
    )
    token = _bearer(keys, "user_p_2", "p2@test")
    client = TestClient(app)
    resp = client.get(
        "/api/v1/billing/portal",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409
    assert "no_stripe_customer" in resp.json()["detail"]
