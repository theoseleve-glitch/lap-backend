"""T159 — Waitlist users get the WAITLIST_FOUNDER promo code
applied automatically at checkout.

Non-waitlist users see no discount. Waitlist users have the
promotion code resolved to its Stripe ID and passed via
`discounts=[{"promotion_code": ...}]` so the discount surfaces in
the Stripe dashboard.
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
    monkeypatch.setenv(
        "STRIPE_PROMO_CODE_WAITLIST_FOUNDER", "WAITLIST_FOUNDER"
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


async def _seed_user(
    factory: Any, sub: str, email: str, *, is_waitlist: bool = False
) -> User:
    async with factory() as session:
        user = User(
            id=uuid6.uuid7(),
            clerk_user_id=sub,
            email=email,
            is_waitlist=is_waitlist,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


def _fake_session() -> Any:
    class FakeSession:
        id = "cs_waitlist"
        url = "https://checkout.stripe.test/wl"

    return FakeSession()


def _fake_promo_list(promo_id: str) -> Any:
    class FakePromo:
        id = promo_id

    class FakeList:
        data = [FakePromo()]

    return FakeList()


async def test_waitlist_user_checkout_applies_promo_code(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_user(factory, "user_wl_1", "wl1@test", is_waitlist=True)
    token = _bearer(keys, "user_wl_1", "wl1@test")

    with patch(
        "stripe.checkout.Session.create",
        return_value=_fake_session(),
    ) as create_mock, patch(
        "stripe.PromotionCode.list",
        return_value=_fake_promo_list("promo_seeded_id"),
    ) as list_mock:
        client = TestClient(app)
        resp = client.post(
            "/api/v1/billing/checkout",
            json={"plan": "waitlist_founder"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    list_mock.assert_called_once()
    list_kwargs = list_mock.call_args.kwargs
    assert list_kwargs["code"] == "WAITLIST_FOUNDER"
    kwargs = create_mock.call_args.kwargs
    assert "discounts" in kwargs
    assert kwargs["discounts"] == [{"promotion_code": "promo_seeded_id"}]


async def test_non_waitlist_user_checkout_has_no_discount(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_user(factory, "user_wl_2", "wl2@test", is_waitlist=False)
    token = _bearer(keys, "user_wl_2", "wl2@test")

    with patch(
        "stripe.checkout.Session.create",
        return_value=_fake_session(),
    ) as create_mock, patch(
        "stripe.PromotionCode.list",
    ) as list_mock:
        client = TestClient(app)
        resp = client.post(
            "/api/v1/billing/checkout",
            json={"plan": "lap_monthly"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    list_mock.assert_not_called()
    kwargs = create_mock.call_args.kwargs
    assert "discounts" not in kwargs


async def test_waitlist_user_with_unresolvable_promo_falls_through(
    configured_app: dict[str, Any],
) -> None:
    # If the promotion code doesn't exist on the Stripe side
    # (admin forgot to create it), the checkout should still
    # succeed — just without a discount. Better than failing the
    # whole signup.
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_user(factory, "user_wl_3", "wl3@test", is_waitlist=True)
    token = _bearer(keys, "user_wl_3", "wl3@test")

    class EmptyList:
        data: list[Any] = []

    with patch(
        "stripe.checkout.Session.create",
        return_value=_fake_session(),
    ) as create_mock, patch(
        "stripe.PromotionCode.list",
        return_value=EmptyList(),
    ):
        client = TestClient(app)
        resp = client.post(
            "/api/v1/billing/checkout",
            json={"plan": "waitlist_founder"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    kwargs = create_mock.call_args.kwargs
    assert "discounts" not in kwargs
