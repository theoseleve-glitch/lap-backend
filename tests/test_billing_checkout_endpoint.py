"""T148 — `POST /api/v1/billing/checkout` creates a Stripe Checkout
session and returns the redirect URL. Authenticated.

Stripe is mocked end-to-end here. T151+ adds webhook signature
verification (real Stripe round-trip), this task verifies only the
"create-session-then-redirect" hop on our side.
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
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": _b64u(n),
        "e": _b64u(e),
    }
    return {"priv_pem": priv_pem, "kid": kid, "jwks": {"keys": [jwk]}}


def _sign(priv_pem: bytes, claims: dict[str, Any], kid: str) -> str:
    return jwt.encode(claims, priv_pem, algorithm="RS256", headers={"kid": kid})


def _bearer(keys: dict[str, Any], sub: str, email: str) -> str:
    now = int(time.time())
    return _sign(
        keys["priv_pem"],
        {"sub": sub, "email": email, "exp": now + 3600},
        keys["kid"],
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
    monkeypatch.setenv("STRIPE_PRICE_LAP_MONTHLY", "price_seeded_monthly")
    monkeypatch.setenv("STRIPE_PRICE_LAP_YEARLY", "price_seeded_yearly")
    monkeypatch.setenv(
        "STRIPE_PRICE_WAITLIST_FOUNDER", "price_seeded_founder"
    )
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


async def _seed_user(factory: Any, sub: str, email: str) -> User:
    async with factory() as session:
        user = User(id=uuid6.uuid7(), clerk_user_id=sub, email=email)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


def _fake_session(url: str = "https://checkout.stripe.test/abc") -> Any:
    class FakeSession:
        def __init__(self) -> None:
            self.id = "cs_test_fake"
            self.url = url

    return FakeSession()


async def test_checkout_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001  (fixture sets up overrides)
) -> None:
    client = TestClient(app)
    resp = client.post(
        "/api/v1/billing/checkout", json={"plan": "lap_monthly"}
    )
    assert resp.status_code == 401


async def test_checkout_returns_url_and_session_id(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_user(factory, "user_chk_1", "chk1@test")
    token = _bearer(keys, "user_chk_1", "chk1@test")

    with patch(
        "stripe.checkout.Session.create",
        return_value=_fake_session("https://checkout.stripe.test/url-1"),
    ) as create_mock:
        client = TestClient(app)
        resp = client.post(
            "/api/v1/billing/checkout",
            json={"plan": "lap_monthly"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["url"] == "https://checkout.stripe.test/url-1"
    assert body["session_id"] == "cs_test_fake"
    create_mock.assert_called_once()


async def test_checkout_uses_correct_price_id_for_each_plan(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_user(factory, "user_chk_2", "chk2@test")
    token = _bearer(keys, "user_chk_2", "chk2@test")
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)

    plan_to_price = {
        "lap_monthly": "price_seeded_monthly",
        "lap_yearly": "price_seeded_yearly",
        "waitlist_founder": "price_seeded_founder",
    }
    for plan, expected_price in plan_to_price.items():
        with patch(
            "stripe.checkout.Session.create",
            return_value=_fake_session(),
        ) as create_mock:
            resp = client.post(
                "/api/v1/billing/checkout",
                json={"plan": plan},
                headers=headers,
            )
        assert resp.status_code == 200, plan
        kwargs = create_mock.call_args.kwargs
        line_items = kwargs["line_items"]
        assert line_items[0]["price"] == expected_price


async def test_checkout_session_is_subscription_mode(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_user(factory, "user_chk_3", "chk3@test")
    token = _bearer(keys, "user_chk_3", "chk3@test")

    with patch(
        "stripe.checkout.Session.create",
        return_value=_fake_session(),
    ) as create_mock:
        client = TestClient(app)
        resp = client.post(
            "/api/v1/billing/checkout",
            json={"plan": "lap_monthly"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    assert create_mock.call_args.kwargs["mode"] == "subscription"


async def test_checkout_invalid_plan_returns_422(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_user(factory, "user_chk_4", "chk4@test")
    token = _bearer(keys, "user_chk_4", "chk4@test")
    client = TestClient(app)
    resp = client.post(
        "/api/v1/billing/checkout",
        json={"plan": "not_a_real_plan"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422
