import base64
import time
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
from app.deps import get_db, get_jwks_client
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import DeviceSession, User

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

    async def fake_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    def fake_jwks_client() -> ClerkJwksClient:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=keys["jwks"])

        return ClerkJwksClient(
            "https://test.clerk", transport=httpx.MockTransport(handler)
        )

    app.dependency_overrides[get_db] = fake_get_db
    app.dependency_overrides[get_jwks_client] = fake_jwks_client
    try:
        yield {"keys": keys, "factory": factory}
    finally:
        app.dependency_overrides.clear()


def _bearer(keys: dict[str, Any], sub: str, email: str) -> str:
    now = int(time.time())
    return _sign(
        keys["priv_pem"],
        {"sub": sub, "email": email, "exp": now + 3600},
        keys["kid"],
    )


async def _seed_user(factory: Any, sub: str, email: str) -> User:
    async with factory() as session:
        user = User(id=uuid6.uuid7(), clerk_user_id=sub, email=email)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _seed_device(
    factory: Any,
    *,
    user_id: Any,
    refresh_hash: str,
    revoked_at: datetime | None = None,
) -> DeviceSession:
    async with factory() as session:
        d = DeviceSession(
            id=uuid6.uuid7(),
            user_id=user_id,
            refresh_token_hash=refresh_hash,
            revoked_at=revoked_at,
        )
        session.add(d)
        await session.commit()
        await session.refresh(d)
        return d


async def test_list_devices_empty_returns_empty_list(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    token = _bearer(keys, "user_devices_empty", "empty@test")
    client = TestClient(app)
    resp = client.get(
        "/api/v1/auth/devices",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_devices_returns_seeded_device(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    user = await _seed_user(factory, "user_one_device", "one@test")
    device = await _seed_device(
        factory, user_id=user.id, refresh_hash="a" * 64
    )

    token = _bearer(keys, "user_one_device", "one@test")
    client = TestClient(app)
    resp = client.get(
        "/api/v1/auth/devices",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["id"] == str(device.id)
    assert "created_at" in body[0]


async def test_list_devices_returns_multiple(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    user = await _seed_user(factory, "user_multi", "multi@test")
    await _seed_device(factory, user_id=user.id, refresh_hash="b" * 64)
    await _seed_device(factory, user_id=user.id, refresh_hash="c" * 64)

    token = _bearer(keys, "user_multi", "multi@test")
    client = TestClient(app)
    resp = client.get(
        "/api/v1/auth/devices",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 2


async def test_list_devices_excludes_other_users(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    me = await _seed_user(factory, "user_me", "me@test")
    other = await _seed_user(factory, "user_other", "other@test")
    await _seed_device(factory, user_id=me.id, refresh_hash="d" * 64)
    await _seed_device(factory, user_id=other.id, refresh_hash="e" * 64)

    token = _bearer(keys, "user_me", "me@test")
    client = TestClient(app)
    resp = client.get(
        "/api/v1/auth/devices",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1


async def test_list_devices_excludes_revoked(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    user = await _seed_user(factory, "user_revoked", "rev@test")
    await _seed_device(factory, user_id=user.id, refresh_hash="f" * 64)
    await _seed_device(
        factory,
        user_id=user.id,
        refresh_hash="0" * 64,
        revoked_at=datetime(2026, 4, 24, tzinfo=UTC),
    )

    token = _bearer(keys, "user_revoked", "rev@test")
    client = TestClient(app)
    resp = client.get(
        "/api/v1/auth/devices",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 1


@pytest.mark.usefixtures("configured_app")
def test_list_devices_without_auth_returns_401() -> None:
    client = TestClient(app)
    resp = client.get("/api/v1/auth/devices")
    assert resp.status_code == 401
