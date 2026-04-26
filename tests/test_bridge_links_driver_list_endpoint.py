"""T021 (sprint2-backend) — GET /api/v1/driver/{id}/bridge/links.

Paginated driver-scoped index of TransferLinks. Returns the same
TransferLink shape as the detail endpoint (T020), wrapped in
{items, total, limit, offset}. Sorted newest-first so the
"recent links" view doesn't need a client-side sort.

Cross-driver isolation: the path's driver_id must match the
authenticated user's profile.id, otherwise 404 (not 403) so
callers can't enumerate drivers.

Pagination: limit (1-100, default 20) and offset (>=0, default 0).
Same dial as catalog endpoints.
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
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


async def _seed_driver(factory: Any, *, clerk_user_id: str) -> dict[str, Any]:
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
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
        await db.commit()
    return {"user_id": user_id, "driver_id": driver_id}


async def _seed_link(
    factory: Any, *, driver_id: Any, created_at: datetime
) -> Any:
    link_id = uuid6.uuid7()
    async with factory() as db:
        db.add(
            TransferLink(
                id=link_id,
                driver_id=driver_id,
                created_at=created_at,
                created_by="auto",
                session_pair=[str(uuid6.uuid7()), str(uuid6.uuid7())],
                link_reason="same_track",
                shape_match_score=0.7,
                intensity_match_score=0.6,
                divergence_points=[],
                transferred_skills=[],
                non_transferred_skills=[],
            )
        )
        await db.commit()
    return link_id


def _list(
    client: TestClient,
    ca: dict[str, Any],
    who: str,
    driver_id: Any,
    **params: Any,
) -> Any:
    return client.get(
        f"/api/v1/driver/{driver_id}/bridge/links",
        headers={"Authorization": f"Bearer {_bearer(ca['keys'], who)}"},
        params=params,
    )


# -- behavior ------------------------------------------------------


def test_list_returns_empty_for_driver_with_no_links(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_e")
    )
    client = TestClient(app)
    resp = _list(client, configured_app, "u_e", seeded["driver_id"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["limit"] == 20
    assert body["offset"] == 0


def test_list_returns_links_for_driver(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_l")
    )
    asyncio.run(
        _seed_link(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            created_at=datetime(2026, 4, 25, 10, tzinfo=UTC),
        )
    )
    asyncio.run(
        _seed_link(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            created_at=datetime(2026, 4, 25, 11, tzinfo=UTC),
        )
    )
    client = TestClient(app)
    resp = _list(client, configured_app, "u_l", seeded["driver_id"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2


def test_list_orders_newest_first(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_o")
    )
    base = datetime(2026, 4, 25, 10, tzinfo=UTC)
    for offset_h in (0, 2, 1):  # seed out of order
        asyncio.run(
            _seed_link(
                configured_app["factory"],
                driver_id=seeded["driver_id"],
                created_at=base + timedelta(hours=offset_h),
            )
        )
    client = TestClient(app)
    resp = _list(client, configured_app, "u_o", seeded["driver_id"])
    timestamps = [item["created_at"] for item in resp.json()["items"]]
    assert timestamps == sorted(timestamps, reverse=True)


def test_list_pagination_limit_and_offset(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_p")
    )
    base = datetime(2026, 4, 25, 10, tzinfo=UTC)
    for i in range(5):
        asyncio.run(
            _seed_link(
                configured_app["factory"],
                driver_id=seeded["driver_id"],
                created_at=base + timedelta(hours=i),
            )
        )
    client = TestClient(app)
    page1 = _list(
        client, configured_app, "u_p", seeded["driver_id"],
        limit=2, offset=0,
    ).json()
    page2 = _list(
        client, configured_app, "u_p", seeded["driver_id"],
        limit=2, offset=2,
    ).json()
    assert page1["total"] == 5
    assert len(page1["items"]) == 2
    assert page2["limit"] == 2
    assert page2["offset"] == 2
    # Pages don't overlap (item ids distinct between page1 and page2)
    page1_ids = {it["id"] for it in page1["items"]}
    page2_ids = {it["id"] for it in page2["items"]}
    assert page1_ids.isdisjoint(page2_ids)


def test_list_does_not_leak_other_drivers_links(
    configured_app: dict[str, Any],
) -> None:
    """Authenticated as u_me, requesting u_other's driver_id ⇒ 404
    (not 403). Same posture as get_session/get_compare_case."""
    other = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_other")
    )
    asyncio.run(
        _seed_link(
            configured_app["factory"],
            driver_id=other["driver_id"],
            created_at=datetime(2026, 4, 25, tzinfo=UTC),
        )
    )
    asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_me")
    )
    client = TestClient(app)
    resp = _list(client, configured_app, "u_me", other["driver_id"])
    assert resp.status_code == 404


def test_list_for_user_without_profile_returns_404(
    configured_app: dict[str, Any],
) -> None:
    client = TestClient(app)
    resp = _list(
        client, configured_app, "u_no_prof", uuid6.uuid7()
    )
    assert resp.status_code == 404


def test_list_invalid_limit_returns_422(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_v")
    )
    client = TestClient(app)
    resp = _list(
        client, configured_app, "u_v", seeded["driver_id"], limit=0
    )
    assert resp.status_code == 422


@pytest.mark.usefixtures("configured_app")
def test_list_unauthenticated_returns_401() -> None:
    client = TestClient(app)
    resp = client.get(f"/api/v1/driver/{uuid6.uuid7()}/bridge/links")
    assert resp.status_code == 401
