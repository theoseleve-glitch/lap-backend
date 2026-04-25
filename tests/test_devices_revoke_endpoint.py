import base64
import time
from collections.abc import AsyncGenerator, Generator
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
from sqlalchemy import select
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
    factory: Any, *, user_id: Any, refresh_hash: str
) -> DeviceSession:
    async with factory() as session:
        d = DeviceSession(
            id=uuid6.uuid7(),
            user_id=user_id,
            refresh_token_hash=refresh_hash,
        )
        session.add(d)
        await session.commit()
        await session.refresh(d)
        return d


async def test_delete_device_returns_204(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    user = await _seed_user(factory, "user_revoke_a", "ra@test")
    device = await _seed_device(
        factory, user_id=user.id, refresh_hash="a" * 64
    )

    client = TestClient(app)
    bearer = _bearer(keys, "user_revoke_a", "ra@test")
    resp = client.delete(
        f"/api/v1/auth/devices/{device.id}",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert resp.status_code == 204


async def test_delete_device_sets_revoked_at_in_db(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    user = await _seed_user(factory, "user_revoke_b", "rb@test")
    device = await _seed_device(
        factory, user_id=user.id, refresh_hash="b" * 64
    )

    client = TestClient(app)
    bearer = _bearer(keys, "user_revoke_b", "rb@test")
    client.delete(
        f"/api/v1/auth/devices/{device.id}",
        headers={"Authorization": f"Bearer {bearer}"},
    )

    async with factory() as session:
        row = (
            await session.execute(
                select(DeviceSession).where(DeviceSession.id == device.id)
            )
        ).scalar_one()
        assert row.revoked_at is not None


async def test_delete_device_removes_from_listing(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    user = await _seed_user(factory, "user_revoke_c", "rc@test")
    device = await _seed_device(
        factory, user_id=user.id, refresh_hash="c" * 64
    )

    client = TestClient(app)
    auth = {"Authorization": f"Bearer {_bearer(keys, 'user_revoke_c', 'rc@test')}"}

    pre = client.get("/api/v1/auth/devices", headers=auth)
    assert len(pre.json()) == 1

    client.delete(f"/api/v1/auth/devices/{device.id}", headers=auth)

    post = client.get("/api/v1/auth/devices", headers=auth)
    assert post.json() == []


async def test_delete_unknown_device_returns_404(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_user(factory, "user_revoke_d", "rd@test")

    client = TestClient(app)
    bearer = _bearer(keys, "user_revoke_d", "rd@test")
    resp = client.delete(
        f"/api/v1/auth/devices/{uuid6.uuid7()}",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert resp.status_code == 404


async def test_delete_other_users_device_returns_404(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    me = await _seed_user(factory, "user_revoke_me", "me@test")
    other = await _seed_user(factory, "user_revoke_other", "other@test")
    other_device = await _seed_device(
        factory, user_id=other.id, refresh_hash="d" * 64
    )

    # `me` exists in DB so get_current_user resolves; the device belongs
    # to `other` so the revoke must not succeed.
    _ = me
    client = TestClient(app)
    resp = client.delete(
        f"/api/v1/auth/devices/{other_device.id}",
        headers={
            "Authorization": f"Bearer {_bearer(keys, 'user_revoke_me', 'me@test')}"
        },
    )
    assert resp.status_code == 404

    # And the row must NOT be revoked.
    async with factory() as session:
        row = (
            await session.execute(
                select(DeviceSession).where(DeviceSession.id == other_device.id)
            )
        ).scalar_one()
        assert row.revoked_at is None


async def test_delete_already_revoked_is_idempotent(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    user = await _seed_user(factory, "user_revoke_idem", "idem@test")
    device = await _seed_device(
        factory, user_id=user.id, refresh_hash="e" * 64
    )

    client = TestClient(app)
    auth = {
        "Authorization": f"Bearer {_bearer(keys, 'user_revoke_idem', 'idem@test')}"
    }
    r1 = client.delete(f"/api/v1/auth/devices/{device.id}", headers=auth)
    r2 = client.delete(f"/api/v1/auth/devices/{device.id}", headers=auth)
    # First DELETE succeeds. After the row is already revoked it
    # is no longer "active" from the caller's perspective, so the
    # second DELETE returns 404 — idempotent in effect (revoked_at
    # stays the same), but explicit about state.
    assert r1.status_code == 204
    assert r2.status_code == 404


@pytest.mark.usefixtures("configured_app")
def test_delete_device_without_auth_returns_401() -> None:
    client = TestClient(app)
    resp = client.delete(f"/api/v1/auth/devices/{uuid6.uuid7()}")
    assert resp.status_code == 401
