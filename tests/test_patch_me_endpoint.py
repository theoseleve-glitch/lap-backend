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
from app.deps import get_db, get_jwks_client
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, User

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
        yield {"keys": keys, "url": url, "factory": factory}
    finally:
        app.dependency_overrides.clear()


def _token(keys: dict[str, Any], sub: str, email: str) -> str:
    now = int(time.time())
    return _sign(
        keys["priv_pem"],
        {"sub": sub, "email": email, "exp": now + 3600},
        keys["kid"],
    )


def test_patch_me_creates_driver_profile_on_first_call(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    token = _token(keys, "user_first_patch", "first@test")
    client = TestClient(app)
    resp = client.patch(
        "/api/v1/me",
        headers={"Authorization": f"Bearer {token}"},
        json={"display_name": "Theo", "skill_level": "intermediate"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["display_name"] == "Theo"
    assert body["skill_level"] == "intermediate"
    assert body["cohort_opt_in"] is False


def test_patch_me_updates_existing_profile(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    token = _token(keys, "user_existing", "existing@test")
    client = TestClient(app)
    r1 = client.patch(
        "/api/v1/me",
        headers={"Authorization": f"Bearer {token}"},
        json={"display_name": "Initial", "skill_level": "beginner"},
    )
    assert r1.status_code == 200
    r2 = client.patch(
        "/api/v1/me",
        headers={"Authorization": f"Bearer {token}"},
        json={"skill_level": "advanced", "cohort_opt_in": True},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["display_name"] == "Initial"
    assert body["skill_level"] == "advanced"
    assert body["cohort_opt_in"] is True


@pytest.mark.usefixtures("configured_app")
def test_patch_me_without_auth_returns_401() -> None:
    client = TestClient(app)
    resp = client.patch("/api/v1/me", json={"display_name": "x"})
    assert resp.status_code == 401


def test_patch_me_first_call_without_display_name_returns_422(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    token = _token(keys, "user_no_name", "noname@test")
    client = TestClient(app)
    resp = client.patch(
        "/api/v1/me",
        headers={"Authorization": f"Bearer {token}"},
        json={"skill_level": "beginner"},
    )
    assert resp.status_code == 422


def test_patch_me_rejects_invalid_skill_level(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    token = _token(keys, "user_bad_level", "bad@test")
    client = TestClient(app)
    resp = client.patch(
        "/api/v1/me",
        headers={"Authorization": f"Bearer {token}"},
        json={"display_name": "x", "skill_level": "super_pro"},
    )
    assert resp.status_code == 422


async def test_patch_me_persists_changes_to_db(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    token = _token(keys, "user_db_persist", "persist@test")
    client = TestClient(app)
    resp = client.patch(
        "/api/v1/me",
        headers={"Authorization": f"Bearer {token}"},
        json={"display_name": "Persisted", "cohort_opt_in": True},
    )
    assert resp.status_code == 200

    async with factory() as session:
        user = (
            await session.execute(
                select(User).where(User.clerk_user_id == "user_db_persist")
            )
        ).scalar_one()
        profile = (
            await session.execute(
                select(DriverProfile).where(DriverProfile.user_id == user.id)
            )
        ).scalar_one()
        assert profile.display_name == "Persisted"
        assert profile.cohort_opt_in is True
