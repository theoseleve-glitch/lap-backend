"""T022 (sprint2-backend) — DELETE /api/v1/bridge/links/{id}.

Revoke a bridge link the authenticated driver owns. 204 on
success, 404 (not 403) on absent/not-owned ids — same posture as
the matching GET (T020) and the compare_cases DELETE.

Deletion is hard, not soft: revoking removes the row outright.
Bridge links are user-curated artifacts; if the user wants the
analysis back they re-create it (or re-accept the auto suggestion).
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
from sqlalchemy import select

from alembic import command
from app.deps import get_db, get_jwks_client
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, TransferLink, User

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
    factory: Any, *, clerk_user_id: str
) -> dict[str, Any]:
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    link_id = uuid6.uuid7()
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
                created_at=datetime(2026, 4, 25, tzinfo=UTC),
                created_by="auto",
                session_pair=[str(uuid6.uuid7()), str(uuid6.uuid7())],
                link_reason="same_track",
                shape_match_score=0.7,
                intensity_match_score=0.6,
            )
        )
        await db.commit()
    return {"link_id": link_id, "driver_id": driver_id}


def _delete(
    client: TestClient, ca: dict[str, Any], who: str, link_id: Any
) -> Any:
    return client.delete(
        f"/api/v1/bridge/links/{link_id}",
        headers={"Authorization": f"Bearer {_bearer(ca['keys'], who)}"},
    )


# -- behavior ------------------------------------------------------


def test_delete_returns_204_on_success(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_link(configured_app["factory"], clerk_user_id="u_d")
    )
    client = TestClient(app)
    resp = _delete(client, configured_app, "u_d", seeded["link_id"])
    assert resp.status_code == 204


def test_delete_removes_row_from_db(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_link(configured_app["factory"], clerk_user_id="u_r")
    )
    client = TestClient(app)
    _delete(client, configured_app, "u_r", seeded["link_id"])

    async def _read() -> Any:
        async with configured_app["factory"]() as db:
            return (
                await db.execute(
                    select(TransferLink).where(
                        TransferLink.id == seeded["link_id"]
                    )
                )
            ).scalar_one_or_none()

    assert asyncio.run(_read()) is None


def test_delete_other_drivers_link_returns_404(
    configured_app: dict[str, Any],
) -> None:
    other = asyncio.run(
        _seed_link(configured_app["factory"], clerk_user_id="u_other")
    )
    asyncio.run(
        _seed_link(configured_app["factory"], clerk_user_id="u_me")
    )
    client = TestClient(app)
    resp = _delete(client, configured_app, "u_me", other["link_id"])
    assert resp.status_code == 404

    # And the other driver's row is still intact (no spillover).
    async def _read() -> Any:
        async with configured_app["factory"]() as db:
            return (
                await db.execute(
                    select(TransferLink).where(
                        TransferLink.id == other["link_id"]
                    )
                )
            ).scalar_one_or_none()

    assert asyncio.run(_read()) is not None


def test_delete_unknown_id_returns_404(
    configured_app: dict[str, Any],
) -> None:
    asyncio.run(
        _seed_link(configured_app["factory"], clerk_user_id="u_x")
    )
    client = TestClient(app)
    resp = _delete(client, configured_app, "u_x", uuid6.uuid7())
    assert resp.status_code == 404


def test_delete_user_without_profile_returns_404(
    configured_app: dict[str, Any],
) -> None:
    client = TestClient(app)
    resp = _delete(client, configured_app, "u_no_prof", uuid6.uuid7())
    assert resp.status_code == 404


@pytest.mark.usefixtures("configured_app")
def test_delete_unauthenticated_returns_401() -> None:
    client = TestClient(app)
    resp = client.delete(f"/api/v1/bridge/links/{uuid6.uuid7()}")
    assert resp.status_code == 401
