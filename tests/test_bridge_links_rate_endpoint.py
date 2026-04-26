"""T023 (sprint2-backend) — POST /api/v1/bridge/links/{id}/rate.

Driver thumbs-up/down on whether a bridge link's analysis was
useful. Server stamps `rated_at`; re-rating overwrites in place.

Cross-driver isolation mirrors the GET (T020) and DELETE (T022) on
the same router: 404 (not 403) for absent or not-owned ids so
callers can't enumerate other drivers' link ids.

Returns the *updated* TransferLink so the client doesn't need a
follow-up GET to refresh state.
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


def _post_rate(
    client: TestClient,
    ca: dict[str, Any],
    who: str,
    link_id: Any,
    body: dict[str, Any],
) -> Any:
    return client.post(
        f"/api/v1/bridge/links/{link_id}/rate",
        json=body,
        headers={"Authorization": f"Bearer {_bearer(ca['keys'], who)}"},
    )


# -- behavior ------------------------------------------------------


def test_rate_returns_200_with_updated_link(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_link(configured_app["factory"], clerk_user_id="u_a")
    )
    client = TestClient(app)
    resp = _post_rate(
        client, configured_app, "u_a", seeded["link_id"], {"rating": "up"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["usefulness_rating"] == "up"
    assert body["rated_at"] is not None
    assert body["id"] == str(seeded["link_id"])


def test_rate_persists_to_db(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_link(configured_app["factory"], clerk_user_id="u_b")
    )
    client = TestClient(app)
    _post_rate(
        client, configured_app, "u_b", seeded["link_id"], {"rating": "down"}
    )

    async def _read() -> Any:
        async with configured_app["factory"]() as db:
            return (
                await db.execute(
                    select(TransferLink).where(
                        TransferLink.id == seeded["link_id"]
                    )
                )
            ).scalar_one()

    row = asyncio.run(_read())
    assert row.usefulness_rating == "down"
    assert row.rated_at is not None


def test_rate_overwrites_existing_rating(
    configured_app: dict[str, Any],
) -> None:
    """Re-rating updates in place — there's only one rating per
    link. The second POST flips up → down and re-stamps rated_at."""
    seeded = asyncio.run(
        _seed_link(configured_app["factory"], clerk_user_id="u_c")
    )
    client = TestClient(app)
    first = _post_rate(
        client, configured_app, "u_c", seeded["link_id"], {"rating": "up"}
    )
    second = _post_rate(
        client, configured_app, "u_c", seeded["link_id"], {"rating": "down"}
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["usefulness_rating"] == "down"
    # rated_at moved forward (or stayed equal — second timestamp
    # must be >= first since wall clock only advances).
    assert second.json()["rated_at"] >= first.json()["rated_at"]


def test_rate_other_drivers_link_returns_404(
    configured_app: dict[str, Any],
) -> None:
    other = asyncio.run(
        _seed_link(configured_app["factory"], clerk_user_id="u_other")
    )
    asyncio.run(
        _seed_link(configured_app["factory"], clerk_user_id="u_me")
    )
    client = TestClient(app)
    resp = _post_rate(
        client, configured_app, "u_me", other["link_id"], {"rating": "up"}
    )
    assert resp.status_code == 404

    # And the other driver's row was not touched (no spillover).
    async def _read() -> Any:
        async with configured_app["factory"]() as db:
            return (
                await db.execute(
                    select(TransferLink).where(
                        TransferLink.id == other["link_id"]
                    )
                )
            ).scalar_one()

    row = asyncio.run(_read())
    assert row.usefulness_rating is None
    assert row.rated_at is None


def test_rate_unknown_id_returns_404(
    configured_app: dict[str, Any],
) -> None:
    asyncio.run(
        _seed_link(configured_app["factory"], clerk_user_id="u_x")
    )
    client = TestClient(app)
    resp = _post_rate(
        client, configured_app, "u_x", uuid6.uuid7(), {"rating": "up"}
    )
    assert resp.status_code == 404


def test_rate_user_without_profile_returns_404(
    configured_app: dict[str, Any],
) -> None:
    client = TestClient(app)
    resp = _post_rate(
        client, configured_app, "u_no_prof", uuid6.uuid7(), {"rating": "up"}
    )
    assert resp.status_code == 404


def test_rate_invalid_rating_returns_422(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_link(configured_app["factory"], clerk_user_id="u_v")
    )
    client = TestClient(app)
    resp = _post_rate(
        client,
        configured_app,
        "u_v",
        seeded["link_id"],
        {"rating": "meh"},  # not in enum
    )
    assert resp.status_code == 422


@pytest.mark.usefixtures("configured_app")
def test_rate_unauthenticated_returns_401() -> None:
    client = TestClient(app)
    resp = client.post(
        f"/api/v1/bridge/links/{uuid6.uuid7()}/rate",
        json={"rating": "up"},
    )
    assert resp.status_code == 401
