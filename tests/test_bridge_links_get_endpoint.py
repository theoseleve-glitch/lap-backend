"""T020 (sprint2-backend) — GET /api/v1/bridge/links/{id}.

Returns the full TransferLink (all ADR-005 §5.5 fields) for a
single bridge link the authenticated driver owns. Companion to
T019 (POST). Same cross-driver isolation posture: 404 (not 403)
for absent or not-owned ids.
"""

from __future__ import annotations

import asyncio
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

from alembic import command
from app.deps import get_db, get_jwks_client
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import (
    DriverProfile,
    TransferLink,
    User,
)

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

    async def fake_get_db() -> AsyncGenerator[Any, None]:
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


async def _seed_link(
    factory: Any,
    *,
    clerk_user_id: str,
) -> dict[str, Any]:
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    link_id = uuid6.uuid7()
    sim_sid = str(uuid6.uuid7())
    real_sid = str(uuid6.uuid7())
    async with factory() as db:
        db.add(
            User(
                id=user_id,
                clerk_user_id=clerk_user_id,
                email=f"{clerk_user_id}@t",
            )
        )
        db.add(
            DriverProfile(id=driver_id, user_id=user_id, display_name="A")
        )
        db.add(
            TransferLink(
                id=link_id,
                driver_id=driver_id,
                created_at=datetime(2026, 4, 25, 12, tzinfo=UTC),
                created_by="auto",
                session_pair=[sim_sid, real_sid],
                link_reason="same_track",
                shape_match_score=0.78,
                intensity_match_score=0.62,
                divergence_points=[
                    {
                        "track_distance_m": 420.5,
                        "metric": "brake_pressure_peak",
                        "sim_value": 0.82,
                        "real_value": 0.95,
                        "divergence_type": "intensity",
                    }
                ],
                transferred_skills=["braking"],
                non_transferred_skills=["tire_management"],
                generated_report_id=None,
            )
        )
        await db.commit()
    return {
        "user_id": user_id,
        "driver_id": driver_id,
        "link_id": link_id,
        "session_pair": [sim_sid, real_sid],
    }


def _get(
    client: TestClient, ca: dict[str, Any], who: str, link_id: Any
) -> Any:
    return client.get(
        f"/api/v1/bridge/links/{link_id}",
        headers={"Authorization": f"Bearer {_bearer(ca['keys'], who)}"},
    )


# -- behavior ------------------------------------------------------


def test_get_returns_full_transfer_link_payload(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_link(configured_app["factory"], clerk_user_id="u_g")
    )
    client = TestClient(app)
    resp = _get(client, configured_app, "u_g", seeded["link_id"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(seeded["link_id"])
    assert body["driver_id"] == str(seeded["driver_id"])
    assert body["created_by"] == "auto"
    assert body["link_reason"] == "same_track"
    assert body["shape_match_score"] == 0.78
    assert body["intensity_match_score"] == 0.62
    assert body["session_pair"] == seeded["session_pair"]
    assert body["transferred_skills"] == ["braking"]
    assert body["non_transferred_skills"] == ["tire_management"]
    assert len(body["divergence_points"]) == 1
    assert body["divergence_points"][0]["metric"] == "brake_pressure_peak"


def test_get_other_drivers_link_returns_404(
    configured_app: dict[str, Any],
) -> None:
    other = asyncio.run(
        _seed_link(configured_app["factory"], clerk_user_id="u_other")
    )
    # Authenticated user has no DriverProfile linked to other's link.
    asyncio.run(
        _seed_link(configured_app["factory"], clerk_user_id="u_me")
    )
    client = TestClient(app)
    resp = _get(client, configured_app, "u_me", other["link_id"])
    assert resp.status_code == 404


def test_get_unknown_id_returns_404(
    configured_app: dict[str, Any],
) -> None:
    asyncio.run(
        _seed_link(configured_app["factory"], clerk_user_id="u_x")
    )
    client = TestClient(app)
    resp = _get(client, configured_app, "u_x", uuid6.uuid7())
    assert resp.status_code == 404


def test_get_user_without_profile_returns_404(
    configured_app: dict[str, Any],
) -> None:
    """A clerk-known user with no DriverProfile yet should still 404
    rather than 401/500 — it's a 'no resource for you' case, not
    'auth broken'."""
    client = TestClient(app)
    resp = _get(client, configured_app, "u_no_prof", uuid6.uuid7())
    assert resp.status_code == 404


@pytest.mark.usefixtures("configured_app")
def test_get_unauthenticated_returns_401() -> None:
    client = TestClient(app)
    resp = client.get(f"/api/v1/bridge/links/{uuid6.uuid7()}")
    assert resp.status_code == 401
