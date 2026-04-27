"""T178 (sprint2-backend) — 30-day trial for B2B orgs.

When the checkout request is for a B2B plan (b2b_5seat,
b2b_15seat, b2b_unlimited), Stripe is configured with a
30-day trial. Consumer plans (lap_monthly, lap_yearly,
waitlist_founder) keep the existing 14-day trial.

Why a longer trial for B2B?
  - B2B sales cycles span multiple users (admin, coaches,
    drivers); the team needs time to onboard before deciding.
  - Consumer trials are individual-user evaluation; 14 days is
    enough to upload a few sessions and see value. B2B is
    "set up the org, invite drivers, see one cohort of
    sessions" which takes longer.

Pinned behaviors:
  - lap_monthly checkout → trial_period_days=14 (regression)
  - lap_yearly checkout → trial_period_days=14 (regression)
  - waitlist_founder checkout → trial_period_days=14
    (regression)
  - b2b_5seat checkout → trial_period_days=30
  - b2b_15seat checkout → trial_period_days=30
  - b2b_unlimited checkout → trial_period_days=30
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
    monkeypatch.setenv("STRIPE_PRICE_LAP_MONTHLY", "price_m")
    monkeypatch.setenv("STRIPE_PRICE_LAP_YEARLY", "price_y")
    monkeypatch.setenv("STRIPE_PRICE_WAITLIST_FOUNDER", "price_f")
    monkeypatch.setenv("STRIPE_PRICE_B2B_5SEAT", "price_5")
    monkeypatch.setenv("STRIPE_PRICE_B2B_15SEAT", "price_15")
    monkeypatch.setenv("STRIPE_PRICE_B2B_UNLIMITED", "price_inf")
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
        id = "cs_t178_fake"
        url = "https://checkout.stripe.test/178"
    return FakeSession()


def _trial_days_for(
    configured_app: dict[str, Any], plan: str, *, sub: str
) -> int:
    """Helper: post a checkout request and return the
    trial_period_days passed to Stripe."""
    keys = configured_app["keys"]
    token = _bearer(keys, sub, f"{sub}@t")
    headers = {"Authorization": f"Bearer {token}"}
    with patch(
        "stripe.checkout.Session.create",
        return_value=_fake_session(),
    ) as create_mock:
        client = TestClient(app)
        resp = client.post(
            "/api/v1/billing/checkout",
            json={"plan": plan},
            headers=headers,
        )
    assert resp.status_code == 200, (plan, resp.text)
    sub_data = create_mock.call_args.kwargs["subscription_data"]
    return int(sub_data["trial_period_days"])


# -- consumer regression -----------------------------------------


async def test_lap_monthly_keeps_14_day_trial(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    await _seed_user(factory, "u_t178a", "u_t178a@t")
    assert _trial_days_for(
        configured_app, "lap_monthly", sub="u_t178a"
    ) == 14


async def test_lap_yearly_keeps_14_day_trial(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    await _seed_user(factory, "u_t178b", "u_t178b@t")
    assert _trial_days_for(
        configured_app, "lap_yearly", sub="u_t178b"
    ) == 14


async def test_waitlist_founder_keeps_14_day_trial(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    await _seed_user(factory, "u_t178c", "u_t178c@t")
    assert _trial_days_for(
        configured_app, "waitlist_founder", sub="u_t178c"
    ) == 14


# -- B2B 30-day trial --------------------------------------------


async def test_b2b_5seat_uses_30_day_trial(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    await _seed_user(factory, "u_t178d", "u_t178d@t")
    assert _trial_days_for(
        configured_app, "b2b_5seat", sub="u_t178d"
    ) == 30


async def test_b2b_15seat_uses_30_day_trial(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    await _seed_user(factory, "u_t178e", "u_t178e@t")
    assert _trial_days_for(
        configured_app, "b2b_15seat", sub="u_t178e"
    ) == 30


async def test_b2b_unlimited_uses_30_day_trial(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    await _seed_user(factory, "u_t178f", "u_t178f@t")
    assert _trial_days_for(
        configured_app, "b2b_unlimited", sub="u_t178f"
    ) == 30
