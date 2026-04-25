"""T162 — `require_active_subscription` is the billing gate.

Composes with `get_current_user`. Returns 200 for `active` or
`trialing` users (Stripe's two access-granting states); raises 402
Payment Required otherwise. `past_due` is gated here too — the 48h
soft-block grace period belongs to T163, not this dep.
"""

import base64
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from alembic import command
from app.deps import get_db, get_jwks_client, require_active_subscription
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


@pytest.fixture
def upgraded_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


def _make_app(db_url: str, jwks_payload: dict[str, Any]) -> FastAPI:
    app = FastAPI()
    engine = make_engine(db_url)
    factory = make_session_factory(engine)

    async def fake_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    def fake_jwks_client() -> ClerkJwksClient:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=jwks_payload)

        return ClerkJwksClient(
            "https://test.clerk", transport=httpx.MockTransport(handler)
        )

    app.dependency_overrides[get_db] = fake_get_db
    app.dependency_overrides[get_jwks_client] = fake_jwks_client

    @app.get("/paid-only")
    async def paid_only(
        user: User = Depends(require_active_subscription),  # noqa: B008
    ) -> dict[str, str]:
        return {"id": str(user.id), "status": user.subscription_status}

    return app


async def _seed_user_with_status(
    db_url: str, sub: str, email: str, status: str
) -> None:
    engine = make_engine(db_url)
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            session.add(
                User(
                    id=uuid6.uuid7(),
                    clerk_user_id=sub,
                    email=email,
                    subscription_status=status,
                )
            )
            await session.commit()
    finally:
        await engine.dispose()


async def test_active_status_passes_through(upgraded_db_url: str) -> None:
    keys = _build_keypair()
    await _seed_user_with_status(
        upgraded_db_url, "user_active", "active@test", "active"
    )
    now = int(time.time())
    token = _sign(
        keys["priv_pem"],
        {"sub": "user_active", "email": "active@test", "exp": now + 3600},
        keys["kid"],
    )
    client = TestClient(_make_app(upgraded_db_url, keys["jwks"]))
    resp = client.get(
        "/paid-only", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"


async def test_trialing_status_passes_through(upgraded_db_url: str) -> None:
    keys = _build_keypair()
    await _seed_user_with_status(
        upgraded_db_url, "user_trial", "trial@test", "trialing"
    )
    now = int(time.time())
    token = _sign(
        keys["priv_pem"],
        {"sub": "user_trial", "email": "trial@test", "exp": now + 3600},
        keys["kid"],
    )
    client = TestClient(_make_app(upgraded_db_url, keys["jwks"]))
    resp = client.get(
        "/paid-only", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "trialing"


async def test_canceled_status_returns_402(upgraded_db_url: str) -> None:
    keys = _build_keypair()
    await _seed_user_with_status(
        upgraded_db_url, "user_canceled", "canceled@test", "canceled"
    )
    now = int(time.time())
    token = _sign(
        keys["priv_pem"],
        {"sub": "user_canceled", "email": "canceled@test", "exp": now + 3600},
        keys["kid"],
    )
    client = TestClient(_make_app(upgraded_db_url, keys["jwks"]))
    resp = client.get(
        "/paid-only", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 402
    assert resp.json()["detail"] == "subscription_required"


async def test_past_due_status_returns_402(upgraded_db_url: str) -> None:
    # T163 will introduce a 48h grace period for past_due via a separate
    # dep / banner. The bare gate here gates strictly.
    keys = _build_keypair()
    await _seed_user_with_status(
        upgraded_db_url, "user_pastdue", "pastdue@test", "past_due"
    )
    now = int(time.time())
    token = _sign(
        keys["priv_pem"],
        {"sub": "user_pastdue", "email": "pastdue@test", "exp": now + 3600},
        keys["kid"],
    )
    client = TestClient(_make_app(upgraded_db_url, keys["jwks"]))
    resp = client.get(
        "/paid-only", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 402


def test_default_inactive_status_returns_402(upgraded_db_url: str) -> None:
    # First call auto-provisions a User with subscription_status=inactive
    # (the column default). No subscription has ever been started.
    keys = _build_keypair()
    now = int(time.time())
    token = _sign(
        keys["priv_pem"],
        {"sub": "user_fresh", "email": "fresh@test", "exp": now + 3600},
        keys["kid"],
    )
    client = TestClient(_make_app(upgraded_db_url, keys["jwks"]))
    resp = client.get(
        "/paid-only", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 402


def test_missing_auth_returns_401(upgraded_db_url: str) -> None:
    # Auth dep runs before the gate; an unauthenticated caller must
    # see 401, not 402, so clients can distinguish "log in" from "pay".
    keys = _build_keypair()
    client = TestClient(_make_app(upgraded_db_url, keys["jwks"]))
    resp = client.get("/paid-only")
    assert resp.status_code == 401
