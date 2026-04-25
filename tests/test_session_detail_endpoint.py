"""T123 — GET /api/v1/sessions/{session_id} exposes status.

Once the Celery task finishes, `Session.status="ready"` is set
(T123 first half). This endpoint lets clients/SSE consumers poll
the row's current state. Authorization: only the session's
owning driver (i.e. the driver_profile linked to the caller's
User row) can read it. Other drivers and unknown sessions both
return 404 — we don't leak existence.
"""

import asyncio
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
from sqlalchemy.ext.asyncio import AsyncSession

from alembic import command
from app.deps import get_db, get_jwks_client
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, User
from infra.orm import Session as SessionRow

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
        async with factory() as db_session:
            yield db_session

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


async def _seed_session(
    factory: Any,
    *,
    clerk_user_id: str,
    email: str,
    status: str,
    failure_reason: str | None = None,
) -> dict[str, Any]:
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    session_id = uuid6.uuid7()
    async with factory() as db:
        db.add(
            User(id=user_id, clerk_user_id=clerk_user_id, email=email)
        )
        db.add(
            DriverProfile(id=driver_id, user_id=user_id, display_name="A")
        )
        db.add(
            SessionRow(
                id=session_id,
                driver_id=driver_id,
                manifest={"bundle_version": "v2"},
                status=status,
                failure_reason=failure_reason,
            )
        )
        await db.commit()
    return {
        "user_id": user_id,
        "driver_id": driver_id,
        "session_id": session_id,
    }


def test_get_session_returns_status_for_owner(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_session(
            configured_app["factory"],
            clerk_user_id="u_owner",
            email="owner@t",
            status="ready",
        )
    )
    keys = configured_app["keys"]
    token = _sign(
        keys["priv_pem"],
        {"sub": "u_owner", "email": "owner@t", "exp": int(time.time()) + 3600},
        keys["kid"],
    )
    client = TestClient(app)
    resp = client.get(
        f"/api/v1/sessions/{seeded['session_id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(seeded["session_id"])
    assert body["status"] == "ready"
    assert body["driver_id"] == str(seeded["driver_id"])


def test_get_session_returns_failure_reason_when_failed(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_session(
            configured_app["factory"],
            clerk_user_id="u_fail",
            email="fail@t",
            status="failed",
            failure_reason="boom",
        )
    )
    keys = configured_app["keys"]
    token = _sign(
        keys["priv_pem"],
        {"sub": "u_fail", "email": "fail@t", "exp": int(time.time()) + 3600},
        keys["kid"],
    )
    client = TestClient(app)
    resp = client.get(
        f"/api/v1/sessions/{seeded['session_id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["failure_reason"] == "boom"


def test_get_session_returns_404_for_unknown_session(
    configured_app: dict[str, Any],
) -> None:
    asyncio.run(
        _seed_session(
            configured_app["factory"],
            clerk_user_id="u_x",
            email="x@t",
            status="ready",
        )
    )
    keys = configured_app["keys"]
    token = _sign(
        keys["priv_pem"],
        {"sub": "u_x", "email": "x@t", "exp": int(time.time()) + 3600},
        keys["kid"],
    )
    bogus = uuid6.uuid7()
    client = TestClient(app)
    resp = client.get(
        f"/api/v1/sessions/{bogus}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


def test_get_session_returns_404_for_other_drivers_session(
    configured_app: dict[str, Any],
) -> None:
    # Owner's session exists; a different authenticated user must not
    # see it. Returning 404 (not 403) avoids leaking existence.
    owner_seeded = asyncio.run(
        _seed_session(
            configured_app["factory"],
            clerk_user_id="u_owner2",
            email="owner2@t",
            status="ready",
        )
    )
    asyncio.run(
        _seed_session(
            configured_app["factory"],
            clerk_user_id="u_intruder",
            email="intruder@t",
            status="ready",
        )
    )

    keys = configured_app["keys"]
    intruder_token = _sign(
        keys["priv_pem"],
        {
            "sub": "u_intruder",
            "email": "intruder@t",
            "exp": int(time.time()) + 3600,
        },
        keys["kid"],
    )
    client = TestClient(app)
    resp = client.get(
        f"/api/v1/sessions/{owner_seeded['session_id']}",
        headers={"Authorization": f"Bearer {intruder_token}"},
    )
    assert resp.status_code == 404


@pytest.mark.usefixtures("configured_app")
def test_get_session_without_auth_returns_401() -> None:
    client = TestClient(app)
    resp = client.get(f"/api/v1/sessions/{uuid6.uuid7()}")
    assert resp.status_code == 401


def test_get_session_with_invalid_uuid_returns_422(
    configured_app: dict[str, Any],
) -> None:
    # FastAPI's path-param validation kicks in before our handler.
    # Use the configured JWKS keypair so auth doesn't mask the 422.
    keys = configured_app["keys"]
    token = _sign(
        keys["priv_pem"],
        {"sub": "u_z", "email": "z@t", "exp": int(time.time()) + 3600},
        keys["kid"],
    )
    client = TestClient(app)
    resp = client.get(
        "/api/v1/sessions/not-a-uuid",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422
