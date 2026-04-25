"""T165 — End-to-end billing happy path.

Walks the full chain: a fresh user authenticates, hits
/billing/checkout (which auto-provisions them and stores their
user_id in the Stripe checkout session metadata), Stripe sends a
`customer.subscription.created` webhook back, our handler updates
the local DB to `subscription_status=trialing`, and a paid-only
endpoint protected by `require_active_subscription` finally lets
them through. Anything in the chain regressing here should fail
loud.
"""

import base64
import hashlib
import hmac
import json
import time
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import jwt
import pytest
from alembic.config import Config as AlembicConfig
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alembic import command
from app.config import Config as AppConfig
from app.deps import (
    get_config,
    get_db,
    get_jwks_client,
    require_active_subscription,
)
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import User

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
WEBHOOK_SECRET = "whsec_e2e_test_secret"


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _build_keypair(kid: str = "e2e-kid") -> dict[str, Any]:
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


def _stripe_signature(payload: bytes, secret: str, timestamp: int) -> str:
    signed_payload = f"{timestamp}.".encode() + payload
    sig = hmac.new(
        secret.encode(), signed_payload, hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={sig}"


def _fake_session(url: str = "https://checkout.stripe.test/e2e") -> Any:
    class FakeSession:
        def __init__(self) -> None:
            self.id = "cs_test_e2e"
            self.url = url

    return FakeSession()


def _subscription_created_event(user_id: str) -> bytes:
    body: dict[str, Any] = {
        "id": "evt_e2e_sub_created",
        "object": "event",
        "type": "customer.subscription.created",
        "data": {
            "object": {
                "id": "sub_e2e_test",
                "object": "subscription",
                "status": "trialing",
                "metadata": {"user_id": user_id},
            }
        },
    }
    return json.dumps(body).encode()


def _post_signed_webhook(payload: bytes) -> Any:
    timestamp = int(time.time())
    sig = _stripe_signature(payload, WEBHOOK_SECRET, timestamp)
    return TestClient(app).post(
        "/api/v1/billing/webhooks",
        content=payload,
        headers={
            "Stripe-Signature": sig,
            "Content-Type": "application/json",
        },
    )


@pytest.fixture
def e2e_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[dict[str, Any], None, None]:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("REDIS_URL", "redis://x")
    monkeypatch.setenv("CLERK_SECRET", "sk_test_clerk")
    monkeypatch.setenv("STRIPE_SECRET", "sk_test_stripe")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
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

    # The production app handles /checkout and /webhooks.
    app.dependency_overrides[get_db] = fake_get_db
    app.dependency_overrides[get_jwks_client] = fake_jwks_client
    app.dependency_overrides[get_config] = fake_config

    # A throwaway sub-app exposes the gated endpoint for assertion.
    # It shares the same DB factory + JWKS so subscription state set
    # by webhooks is visible from this app's gate dep.
    gate_app = FastAPI()
    gate_app.dependency_overrides[get_db] = fake_get_db
    gate_app.dependency_overrides[get_jwks_client] = fake_jwks_client

    @gate_app.get("/paid-only")
    async def paid_only(
        user: User = Depends(require_active_subscription),  # noqa: B008
    ) -> dict[str, str]:
        return {"status": user.subscription_status}

    try:
        yield {"keys": keys, "factory": factory, "gate_app": gate_app}
    finally:
        app.dependency_overrides.clear()


async def test_checkout_to_webhook_to_gated_endpoint_happy_path(
    e2e_env: dict[str, Any],
) -> None:
    keys = e2e_env["keys"]
    factory = e2e_env["factory"]
    gate_app = e2e_env["gate_app"]
    token = _bearer(keys, "user_e2e", "e2e@test")
    auth = {"Authorization": f"Bearer {token}"}

    # Step 1: Before any subscription activity, the gate must reject.
    # The user is auto-provisioned by /me-style auth call, getting
    # subscription_status=inactive.
    pre_resp = TestClient(gate_app).get("/paid-only", headers=auth)
    assert pre_resp.status_code == 402, (
        "gate let an inactive user through before checkout fired"
    )

    # Step 2: Hit /checkout — Stripe is mocked, but our endpoint
    # captures the user.id and stuffs it into session.metadata for
    # the webhook to read back.
    with patch(
        "stripe.checkout.Session.create", return_value=_fake_session()
    ) as create_mock:
        resp = TestClient(app).post(
            "/api/v1/billing/checkout",
            json={"plan": "lap_monthly"},
            headers=auth,
        )
    assert resp.status_code == 200
    assert resp.json()["url"] == "https://checkout.stripe.test/e2e"
    metadata_sent = create_mock.call_args.kwargs["metadata"]
    user_id_in_metadata = metadata_sent["user_id"]

    # Step 3: The DB now has a user row whose id matches what we
    # forwarded to Stripe — verifies the auto-provision wiring.
    async with factory() as session:
        user_row = (
            await session.execute(
                select(User).where(User.clerk_user_id == "user_e2e")
            )
        ).scalar_one()
        assert str(user_row.id) == user_id_in_metadata
        assert user_row.subscription_status == "inactive"

    # Step 4: Stripe sends `customer.subscription.created` webhook.
    payload = _subscription_created_event(user_id_in_metadata)
    webhook_resp = _post_signed_webhook(payload)
    assert webhook_resp.status_code == 200

    # Step 5: DB updated to trialing.
    async with factory() as session:
        refreshed = (
            await session.execute(
                select(User).where(User.id == user_row.id)
            )
        ).scalar_one()
        assert refreshed.subscription_status == "trialing"

    # Step 6: Same caller can now reach the paid-only endpoint.
    post_resp = TestClient(gate_app).get("/paid-only", headers=auth)
    assert post_resp.status_code == 200
    assert post_resp.json()["status"] == "trialing"
