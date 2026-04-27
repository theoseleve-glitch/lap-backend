"""T177 (sprint2-backend) — B2B pricing tiers: 5-seat / 15-seat /
unlimited.

Three new `BillingPlan` literals route to three new Stripe price
IDs (created via the Stripe dashboard; configured via env vars
on each deploy):

  - `b2b_5seat`    → `STRIPE_PRICE_B2B_5SEAT`
  - `b2b_15seat`   → `STRIPE_PRICE_B2B_15SEAT`
  - `b2b_unlimited` → `STRIPE_PRICE_B2B_UNLIMITED`

The seat-cap enforcement (T170 402) lives on the Organization
schema, not the price tier — the Stripe price + the org's
`seats_limit` are configured together at provisioning time. This
test pins the wire-level mapping plan_literal → price_id.

Pinned behaviors:
  - each B2B plan routes to its configured price_id in the
    checkout-create call
  - each plan validates (no 422) when posted in the body
  - missing config (price empty) → 503 (consistent with the
    existing consumer-tier behavior in _resolve_price_id)
"""

from __future__ import annotations

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
    monkeypatch.setenv(
        "STRIPE_PRICE_B2B_5SEAT", "price_seeded_b2b_5"
    )
    monkeypatch.setenv(
        "STRIPE_PRICE_B2B_15SEAT", "price_seeded_b2b_15"
    )
    monkeypatch.setenv(
        "STRIPE_PRICE_B2B_UNLIMITED", "price_seeded_b2b_inf"
    )
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")

    keys = _build_keypair()
    engine = make_engine(url)
    factory = make_session_factory(engine)

    async def fake_get_db() -> AsyncGenerator[Any, None]:
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


async def _seed_user(factory: Any, sub: str, email: str) -> None:
    async with factory() as session:
        session.add(
            User(id=uuid6.uuid7(), clerk_user_id=sub, email=email)
        )
        await session.commit()


def _fake_session() -> Any:
    class FakeSession:
        id = "cs_b2b_fake"
        url = "https://checkout.stripe.test/b2b"
    return FakeSession()


# -- happy paths -------------------------------------------------


async def test_b2b_plans_route_to_configured_prices(
    configured_app: dict[str, Any],
) -> None:
    """Each of the 3 new B2B plans routes to its env-configured
    price_id. Pin: plan_literal → price_id is the API contract,
    and dashboard-side product creation is meaningless without
    this routing."""
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_user(factory, "user_b2b_1", "b2b1@test")
    token = _bearer(keys, "user_b2b_1", "b2b1@test")
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)

    plan_to_price = {
        "b2b_5seat": "price_seeded_b2b_5",
        "b2b_15seat": "price_seeded_b2b_15",
        "b2b_unlimited": "price_seeded_b2b_inf",
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
        assert kwargs["line_items"][0]["price"] == expected_price


# -- validation --------------------------------------------------


async def test_b2b_plan_validates_at_schema_layer(
    configured_app: dict[str, Any],
) -> None:
    """All 3 new plan literals pass schema validation. Pin: the
    BillingPlan Literal is the source of truth for the closed
    set."""
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_user(factory, "user_b2b_2", "b2b2@test")
    token = _bearer(keys, "user_b2b_2", "b2b2@test")
    client = TestClient(app)

    for plan in ("b2b_5seat", "b2b_15seat", "b2b_unlimited"):
        with patch(
            "stripe.checkout.Session.create",
            return_value=_fake_session(),
        ):
            resp = client.post(
                "/api/v1/billing/checkout",
                json={"plan": plan},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code != 422, plan


# -- missing config ----------------------------------------------


@pytest.fixture
def app_with_no_b2b_prices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[dict[str, Any], None, None]:
    """Variant of `configured_app` with B2B price envs
    deliberately unset to test the missing-config 503 path."""
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("REDIS_URL", "redis://x")
    monkeypatch.setenv("CLERK_SECRET", "sk_test_clerk")
    monkeypatch.setenv("STRIPE_SECRET", "sk_test_stripe")
    # B2B price envs intentionally unset.
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    keys = _build_keypair()
    engine = make_engine(url)
    factory = make_session_factory(engine)

    async def fake_get_db() -> AsyncGenerator[Any, None]:
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


async def test_b2b_plan_with_unconfigured_price_returns_503(
    app_with_no_b2b_prices: dict[str, Any],
) -> None:
    """If a B2B price ID isn't set in env, the endpoint surfaces
    503 (matches the existing consumer-tier behavior in
    _resolve_price_id). Pin: misconfigured deploys fail loudly
    rather than silently routing to the wrong product."""
    keys = app_with_no_b2b_prices["keys"]
    factory = app_with_no_b2b_prices["factory"]
    await _seed_user(factory, "user_b2b_unconf", "u@test")
    token = _bearer(keys, "user_b2b_unconf", "u@test")
    client = TestClient(app)
    resp = client.post(
        "/api/v1/billing/checkout",
        json={"plan": "b2b_5seat"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 503
