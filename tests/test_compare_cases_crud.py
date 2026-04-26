"""T005 (sprint2-backend) — Compare Case CRUD endpoints.

POST /api/v1/compare_cases       creates a saved comparison
GET  /api/v1/compare_cases/{id}  returns the case
DELETE /api/v1/compare_cases/{id} removes it (204)

Cross-driver isolation: cases are scoped to the caller's
driver_id. Accessing another driver's case returns 404 (not
403) so attackers can't enumerate IDs they don't own —
mirrors the prescriptions / sessions pattern.
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


async def _seed_driver(
    factory: Any, *, clerk_user_id: str, email: str
) -> dict[str, Any]:
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    async with factory() as db:
        db.add(User(id=user_id, clerk_user_id=clerk_user_id, email=email))
        db.add(DriverProfile(id=driver_id, user_id=user_id, display_name="A"))
        await db.commit()
    return {"user_id": user_id, "driver_id": driver_id}


def _bearer(keys: dict[str, Any], clerk_user_id: str) -> str:
    return _sign(
        keys["priv_pem"],
        {
            "sub": clerk_user_id,
            "email": f"{clerk_user_id}@t",
            "exp": int(time.time()) + 3600,
        },
        keys["kid"],
    )


def _two_session_ids() -> list[str]:
    return [str(uuid6.uuid7()), str(uuid6.uuid7())]


# -- POST --------------------------------------------------------


def test_post_creates_case_and_returns_201(
    configured_app: dict[str, Any],
) -> None:
    asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_post", email="p@t")
    )
    client = TestClient(app)
    body = {
        "session_ids": _two_session_ids(),
        "rationale": "why was Spa slower",
    }
    resp = client.post(
        "/api/v1/compare_cases",
        json=body,
        headers={
            "Authorization": (
                f"Bearer {_bearer(configured_app['keys'], 'u_post')}"
            )
        },
    )
    assert resp.status_code == 201
    payload = resp.json()
    assert payload["rationale"] == "why was Spa slower"
    assert len(payload["session_ids"]) == 2
    assert "id" in payload
    assert "driver_id" in payload
    assert "created_at" in payload


def test_post_rejects_single_session_with_422(
    configured_app: dict[str, Any],
) -> None:
    asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_one", email="o@t")
    )
    client = TestClient(app)
    resp = client.post(
        "/api/v1/compare_cases",
        json={"session_ids": [str(uuid6.uuid7())], "rationale": "x"},
        headers={
            "Authorization": (
                f"Bearer {_bearer(configured_app['keys'], 'u_one')}"
            )
        },
    )
    assert resp.status_code in (422, 400)


@pytest.mark.usefixtures("configured_app")
def test_post_without_auth_returns_401() -> None:
    client = TestClient(app)
    resp = client.post(
        "/api/v1/compare_cases",
        json={"session_ids": _two_session_ids(), "rationale": "x"},
    )
    assert resp.status_code == 401


# -- GET ---------------------------------------------------------


def test_get_returns_created_case(configured_app: dict[str, Any]) -> None:
    asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_get", email="g@t")
    )
    client = TestClient(app)
    headers = {
        "Authorization": f"Bearer {_bearer(configured_app['keys'], 'u_get')}"
    }
    created = client.post(
        "/api/v1/compare_cases",
        json={"session_ids": _two_session_ids(), "rationale": "rt"},
        headers=headers,
    ).json()
    case_id = created["id"]
    resp = client.get(f"/api/v1/compare_cases/{case_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == case_id


def test_get_unknown_id_returns_404(configured_app: dict[str, Any]) -> None:
    asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_404", email="x@t")
    )
    client = TestClient(app)
    resp = client.get(
        f"/api/v1/compare_cases/{uuid6.uuid7()}",
        headers={
            "Authorization": (
                f"Bearer {_bearer(configured_app['keys'], 'u_404')}"
            )
        },
    )
    assert resp.status_code == 404


def test_get_other_drivers_case_returns_404(
    configured_app: dict[str, Any],
) -> None:
    """Cross-driver isolation: 404, not 403, so the attacker
    cannot distinguish 'exists but mine' from 'truly absent'."""
    asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_a", email="a@t")
    )
    asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_b", email="b@t")
    )
    client = TestClient(app)
    headers_a = {
        "Authorization": f"Bearer {_bearer(configured_app['keys'], 'u_a')}"
    }
    headers_b = {
        "Authorization": f"Bearer {_bearer(configured_app['keys'], 'u_b')}"
    }
    created = client.post(
        "/api/v1/compare_cases",
        json={"session_ids": _two_session_ids(), "rationale": "a's case"},
        headers=headers_a,
    ).json()
    resp = client.get(
        f"/api/v1/compare_cases/{created['id']}", headers=headers_b
    )
    assert resp.status_code == 404


# -- DELETE ------------------------------------------------------


def test_delete_returns_204_and_removes(
    configured_app: dict[str, Any],
) -> None:
    asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_del", email="d@t")
    )
    client = TestClient(app)
    headers = {
        "Authorization": f"Bearer {_bearer(configured_app['keys'], 'u_del')}"
    }
    created = client.post(
        "/api/v1/compare_cases",
        json={"session_ids": _two_session_ids(), "rationale": "x"},
        headers=headers,
    ).json()
    case_id = created["id"]
    delete_resp = client.delete(
        f"/api/v1/compare_cases/{case_id}", headers=headers
    )
    assert delete_resp.status_code == 204
    follow_up = client.get(f"/api/v1/compare_cases/{case_id}", headers=headers)
    assert follow_up.status_code == 404


def test_delete_other_drivers_case_returns_404(
    configured_app: dict[str, Any],
) -> None:
    asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_da", email="da@t")
    )
    asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_db", email="db@t")
    )
    client = TestClient(app)
    headers_a = {
        "Authorization": f"Bearer {_bearer(configured_app['keys'], 'u_da')}"
    }
    headers_b = {
        "Authorization": f"Bearer {_bearer(configured_app['keys'], 'u_db')}"
    }
    created = client.post(
        "/api/v1/compare_cases",
        json={"session_ids": _two_session_ids(), "rationale": "a"},
        headers=headers_a,
    ).json()
    resp = client.delete(
        f"/api/v1/compare_cases/{created['id']}", headers=headers_b
    )
    assert resp.status_code == 404
    # And A can still see their case — deletion didn't leak.
    still_there = client.get(
        f"/api/v1/compare_cases/{created['id']}", headers=headers_a
    )
    assert still_there.status_code == 200
