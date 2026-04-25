"""T095 — full happy-path integration test.

Walks the realistic flow a user goes through:
  1. Clerk webhook fires `user.created`              → DB row created
  2. Webapp/mobile signs a JWT and calls GET /me     → 200 with identity
  3. PATCH /me to set profile fields                  → 200, profile created
  4. POST /api/v1/auth/pairing/create                 → 200 with pairing_token
  5. POST /api/v1/auth/pairing/redeem                 → 200 with refresh_token

The "upload" leg from the sprint card (signup→me→pair→upload) is left out
of scope here because the ingestion endpoint lands in T096; this test will
be extended to cover it then.
"""
import base64
import time
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from alembic.config import Config as AlembicConfig
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alembic import command
from app.deps import get_db, get_jwks_client, get_pairing_store
from app.main import app
from app.routers.webhooks import verify_clerk_webhook_signature
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, User
from infra.pairing_store import PairingStore

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


@pytest.fixture
def configured_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[dict[str, Any], None, None]:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")

    keys = _build_keypair()
    engine = make_engine(url)
    factory = make_session_factory(engine)
    pairing_store = PairingStore()

    async def fake_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    def fake_jwks_client() -> ClerkJwksClient:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=keys["jwks"])

        return ClerkJwksClient(
            "https://test.clerk", transport=httpx.MockTransport(handler)
        )

    async def bypass_signature() -> None:
        return None

    app.dependency_overrides[get_db] = fake_get_db
    app.dependency_overrides[get_jwks_client] = fake_jwks_client
    app.dependency_overrides[get_pairing_store] = lambda: pairing_store
    app.dependency_overrides[verify_clerk_webhook_signature] = bypass_signature
    try:
        yield {"keys": keys, "factory": factory}
    finally:
        app.dependency_overrides.clear()


def _user_jwt(keys: dict[str, Any], sub: str, email: str) -> str:
    now = int(time.time())
    return _sign(
        keys["priv_pem"],
        {"sub": sub, "email": email, "exp": now + 3600},
        keys["kid"],
    )


async def test_full_happy_path_signup_me_pair(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    client = TestClient(app)

    clerk_user_id = "user_clerk_happy"
    email = "happy@test"

    # 1. Clerk webhook fires user.created → backend creates the User row.
    webhook = client.post(
        "/api/v1/auth/webhooks",
        json={
            "object": "event",
            "type": "user.created",
            "data": {
                "id": clerk_user_id,
                "primary_email_address_id": "idn_1",
                "email_addresses": [
                    {"id": "idn_1", "email_address": email}
                ],
            },
        },
    )
    assert webhook.status_code == 200

    async with factory() as session:
        user_row = (
            await session.execute(
                select(User).where(User.clerk_user_id == clerk_user_id)
            )
        ).scalar_one()
        assert user_row.email == email

    # 2. User signs in (Clerk-issued JWT). GET /api/v1/me returns identity.
    bearer = _user_jwt(keys, clerk_user_id, email)
    me = client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {bearer}"}
    )
    assert me.status_code == 200
    me_body = me.json()
    assert me_body["clerk_user_id"] == clerk_user_id
    assert me_body["email"] == email
    assert me_body["is_admin"] is False
    assert me_body["subscription_status"] == "inactive"

    # 3. PATCH /api/v1/me to set profile fields → DriverProfile auto-provisioned.
    patch = client.patch(
        "/api/v1/me",
        headers={"Authorization": f"Bearer {bearer}"},
        json={"display_name": "Theo", "skill_level": "intermediate"},
    )
    assert patch.status_code == 200
    profile = patch.json()
    assert profile["display_name"] == "Theo"
    assert profile["skill_level"] == "intermediate"

    async with factory() as session:
        driver = (
            await session.execute(
                select(DriverProfile).where(
                    DriverProfile.user_id == user_row.id
                )
            )
        ).scalar_one()
        assert driver.display_name == "Theo"

    # 4. Webapp creates a pairing token (for desktop QR handoff).
    create = client.post(
        "/api/v1/auth/pairing/create",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert create.status_code == 200
    pairing_token = create.json()["pairing_token"]
    assert len(pairing_token) >= 32

    # 5. Desktop redeems the token (no auth needed — possesses the QR).
    redeem = client.post(
        "/api/v1/auth/pairing/redeem",
        json={"pairing_token": pairing_token},
    )
    assert redeem.status_code == 200
    refresh_token = redeem.json()["refresh_token"]
    assert len(refresh_token) >= 32

    # Pairing tokens are single-use: a second redeem must 401.
    redeem_again = client.post(
        "/api/v1/auth/pairing/redeem",
        json={"pairing_token": pairing_token},
    )
    assert redeem_again.status_code == 401
