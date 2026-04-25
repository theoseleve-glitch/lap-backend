"""T149 — Checkout session metadata includes `user_id`, trial=14 days.

Verifies the kwargs passed to `stripe.checkout.Session.create`
include `metadata.user_id` (so it surfaces on every webhook event
for that subscription) and `subscription_data.trial_period_days=14`.
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


def _fake_session() -> Any:
    class FakeSession:
        id = "cs_test_meta"
        url = "https://checkout.stripe.test/meta"

    return FakeSession()


async def test_checkout_metadata_includes_user_id(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    user = await _seed_user(factory, "user_meta_1", "meta1@test")
    token = _bearer(keys, "user_meta_1", "meta1@test")

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
    kwargs = create_mock.call_args.kwargs
    assert "metadata" in kwargs
    assert kwargs["metadata"]["user_id"] == str(user.id)


async def test_checkout_subscription_data_has_14_day_trial(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_user(factory, "user_meta_2", "meta2@test")
    token = _bearer(keys, "user_meta_2", "meta2@test")

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
    kwargs = create_mock.call_args.kwargs
    assert "subscription_data" in kwargs
    assert kwargs["subscription_data"]["trial_period_days"] == 14


async def test_checkout_metadata_persists_across_plans(
    configured_app: dict[str, Any],
) -> None:
    # Whichever plan the user picks, metadata.user_id and the trial
    # are present — billing introspection shouldn't depend on plan.
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    user = await _seed_user(factory, "user_meta_3", "meta3@test")
    token = _bearer(keys, "user_meta_3", "meta3@test")
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    for plan in ("lap_monthly", "lap_yearly", "waitlist_founder"):
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
        assert kwargs["metadata"]["user_id"] == str(user.id), plan
        assert kwargs["subscription_data"]["trial_period_days"] == 14, plan
