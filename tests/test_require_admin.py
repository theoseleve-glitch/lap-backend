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
from app.deps import get_db, get_jwks_client, require_admin
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

    @app.get("/admin-only")
    async def admin_only(
        user: User = Depends(require_admin),  # noqa: B008
    ) -> dict[str, str]:
        return {"id": str(user.id), "email": user.email}

    return app


async def _seed_admin(db_url: str, sub: str, email: str) -> None:
    engine = make_engine(db_url)
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            session.add(
                User(
                    id=uuid6.uuid7(),
                    clerk_user_id=sub,
                    email=email,
                    is_admin=True,
                )
            )
            await session.commit()
    finally:
        await engine.dispose()


async def test_admin_user_passes_through(upgraded_db_url: str) -> None:
    keys = _build_keypair()
    await _seed_admin(upgraded_db_url, "user_admin", "admin@test")
    now = int(time.time())
    token = _sign(
        keys["priv_pem"],
        {"sub": "user_admin", "email": "admin@test", "exp": now + 3600},
        keys["kid"],
    )
    client = TestClient(_make_app(upgraded_db_url, keys["jwks"]))
    resp = client.get(
        "/admin-only", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["email"] == "admin@test"


def test_non_admin_user_returns_403(upgraded_db_url: str) -> None:
    keys = _build_keypair()
    now = int(time.time())
    # First call auto-provisions a User with is_admin=False (default).
    token = _sign(
        keys["priv_pem"],
        {"sub": "user_plain", "email": "plain@test", "exp": now + 3600},
        keys["kid"],
    )
    client = TestClient(_make_app(upgraded_db_url, keys["jwks"]))
    resp = client.get(
        "/admin-only", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403


def test_missing_auth_returns_401(upgraded_db_url: str) -> None:
    keys = _build_keypair()
    client = TestClient(_make_app(upgraded_db_url, keys["jwks"]))
    resp = client.get("/admin-only")
    assert resp.status_code == 401


def test_invalid_token_returns_401(upgraded_db_url: str) -> None:
    keys = _build_keypair()
    client = TestClient(_make_app(upgraded_db_url, keys["jwks"]))
    resp = client.get(
        "/admin-only", headers={"Authorization": "Bearer not.a.real.jwt"}
    )
    assert resp.status_code == 401
