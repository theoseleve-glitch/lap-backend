"""T164 — `POST /api/v1/admin/billing/refund` issues a Stripe refund.

Admin-only support tool. Operator forwards a charge_id (and an
optional partial amount in cents) and the endpoint relays it to
Stripe. We don't look up the user, attach the refund to the local
DB, or reverse subscription state — that's the operator's call to
make manually based on the support ticket.
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
    factory: Any, sub: str, email: str, *, is_admin: bool = False
) -> None:
    async with factory() as session:
        session.add(
            User(
                id=uuid6.uuid7(),
                clerk_user_id=sub,
                email=email,
                is_admin=is_admin,
            )
        )
        await session.commit()


def test_refund_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/billing/refund", json={"charge_id": "ch_x"}
    )
    assert resp.status_code == 401


async def test_refund_non_admin_returns_403(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_user(
        factory, "user_caller_404", "caller404@test", is_admin=False
    )
    token = _bearer(keys, "user_caller_404", "caller404@test")
    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/billing/refund",
        headers={"Authorization": f"Bearer {token}"},
        json={"charge_id": "ch_x"},
    )
    assert resp.status_code == 403


async def test_refund_missing_charge_id_returns_422(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_user(
        factory, "user_admin_422", "admin422@test", is_admin=True
    )
    token = _bearer(keys, "user_admin_422", "admin422@test")
    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/billing/refund",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert resp.status_code == 422


async def test_refund_full_amount_relays_to_stripe(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_user(
        factory, "user_admin_full", "admin_full@test", is_admin=True
    )
    token = _bearer(keys, "user_admin_full", "admin_full@test")

    fake_refund = {
        "id": "re_test_full",
        "status": "succeeded",
        "amount": 1500,
        "currency": "eur",
    }
    with patch(
        "stripe.Refund.create", return_value=fake_refund
    ) as mock_create:
        client = TestClient(app)
        resp = client.post(
            "/api/v1/admin/billing/refund",
            headers={"Authorization": f"Bearer {token}"},
            json={"charge_id": "ch_test_full"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["refund_id"] == "re_test_full"
    assert body["status"] == "succeeded"
    assert body["amount"] == 1500
    # No `amount` was passed → full refund (Stripe default).
    mock_create.assert_called_once_with(charge="ch_test_full")


async def test_refund_partial_amount_relays_to_stripe(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_user(
        factory, "user_admin_part", "admin_part@test", is_admin=True
    )
    token = _bearer(keys, "user_admin_part", "admin_part@test")

    fake_refund = {
        "id": "re_test_part",
        "status": "succeeded",
        "amount": 500,
        "currency": "eur",
    }
    with patch(
        "stripe.Refund.create", return_value=fake_refund
    ) as mock_create:
        client = TestClient(app)
        resp = client.post(
            "/api/v1/admin/billing/refund",
            headers={"Authorization": f"Bearer {token}"},
            json={"charge_id": "ch_test_part", "amount": 500},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["amount"] == 500
    mock_create.assert_called_once_with(charge="ch_test_part", amount=500)
