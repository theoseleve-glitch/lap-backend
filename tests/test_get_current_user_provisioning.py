import base64
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

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
from app.deps import get_current_user, get_db, get_jwks_client
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

    @app.get("/me")
    async def me(
        user: User = Depends(get_current_user),  # noqa: B008
    ) -> dict[str, str]:
        return {
            "id": str(user.id),
            "clerk_user_id": user.clerk_user_id,
            "email": user.email,
        }

    return app


def test_first_call_provisions_a_user_row(upgraded_db_url: str) -> None:
    keys = _build_keypair()
    now = int(time.time())
    token = _sign(
        keys["priv_pem"],
        {"sub": "user_clerk_abc", "email": "abc@test", "exp": now + 3600},
        keys["kid"],
    )
    client = TestClient(_make_app(upgraded_db_url, keys["jwks"]))
    resp = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["clerk_user_id"] == "user_clerk_abc"
    assert body["email"] == "abc@test"


def test_second_call_returns_existing_row_without_duplicate(
    upgraded_db_url: str,
) -> None:
    keys = _build_keypair()
    now = int(time.time())
    token = _sign(
        keys["priv_pem"],
        {"sub": "user_clerk_dup", "email": "dup@test", "exp": now + 3600},
        keys["kid"],
    )
    client = TestClient(_make_app(upgraded_db_url, keys["jwks"]))
    r1 = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    r2 = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]


def test_different_sub_provisions_separate_users(upgraded_db_url: str) -> None:
    keys = _build_keypair()
    now = int(time.time())
    t1 = _sign(
        keys["priv_pem"],
        {"sub": "user_alpha", "email": "a@test", "exp": now + 3600},
        keys["kid"],
    )
    t2 = _sign(
        keys["priv_pem"],
        {"sub": "user_beta", "email": "b@test", "exp": now + 3600},
        keys["kid"],
    )
    client = TestClient(_make_app(upgraded_db_url, keys["jwks"]))
    r1 = client.get("/me", headers={"Authorization": f"Bearer {t1}"})
    r2 = client.get("/me", headers={"Authorization": f"Bearer {t2}"})
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["id"] != r2.json()["id"]


def test_jwt_without_email_returns_401(upgraded_db_url: str) -> None:
    keys = _build_keypair()
    now = int(time.time())
    token = _sign(
        keys["priv_pem"],
        {"sub": "user_no_email", "exp": now + 3600},
        keys["kid"],
    )
    client = TestClient(_make_app(upgraded_db_url, keys["jwks"]))
    resp = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


async def test_first_call_writes_to_database(upgraded_db_url: str) -> None:
    keys = _build_keypair()
    now = int(time.time())
    token = _sign(
        keys["priv_pem"],
        {"sub": "user_db_check", "email": "check@test", "exp": now + 3600},
        keys["kid"],
    )
    client = TestClient(_make_app(upgraded_db_url, keys["jwks"]))
    resp = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200

    engine = make_engine(upgraded_db_url)
    try:
        factory = make_session_factory(engine)
        async with factory() as session:
            rows = (
                await session.execute(
                    select(User).where(User.clerk_user_id == "user_db_check")
                )
            ).scalars().all()
            assert len(rows) == 1
            assert rows[0].email == "check@test"
    finally:
        await engine.dispose()
