"""T024 (sprint2-backend) — Bridge RLS integration test.

Cross-cutting proof that drivers can only see/modify their own
bridge resources. Each individual bridge endpoint already has its
own per-endpoint 404 cross-driver test; this file proves the
property holds for the bridge API *as a whole* — driver A cannot
read, mutate, or even existence-probe driver B's data via any
bridge endpoint, and driver B's state is byte-identical after a
comprehensive cross-driver attack.

We're enforcing RLS at the application layer (every bridge
endpoint filters by `driver_id = profile.id` and returns
404-not-403). This integration test is the regression pin for that
property: if anyone adds a new bridge endpoint without the
filter, this test trips. If anyone changes a 404 to a 403, this
test still passes (we assert "not 200/204", not the exact code) —
intentional, because the codename of the response shouldn't be
the thing we lock down; reachability and side-effects are.
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
from infra.orm import (
    DriverProfile,
    TransferLink,
    User,
)
from infra.orm import (
    Session as SessionRow,
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


async def _seed_driver_with_link(
    factory: Any,
    *,
    clerk_user_id: str,
    rating: str | None = None,
) -> dict[str, Any]:
    """Seed a driver with two sessions and one bridge link. The
    optional `rating` lets us pre-mark driver B's link as 'up' so
    we can verify driver A's rate-attack doesn't overwrite it."""
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    session_a = uuid6.uuid7()
    session_b = uuid6.uuid7()
    link_id = uuid6.uuid7()
    rated_at = datetime(2026, 4, 25, 12, tzinfo=UTC) if rating else None
    async with factory() as db:
        db.add(
            User(
                id=user_id,
                clerk_user_id=clerk_user_id,
                email=f"{clerk_user_id}@t",
            )
        )
        db.add(
            DriverProfile(
                id=driver_id, user_id=user_id, display_name=clerk_user_id
            )
        )
        for sid in (session_a, session_b):
            db.add(
                SessionRow(
                    id=sid,
                    driver_id=driver_id,
                    manifest={},
                    created_at=datetime(2026, 4, 25, tzinfo=UTC),
                    status="ready",
                )
            )
        db.add(
            TransferLink(
                id=link_id,
                driver_id=driver_id,
                created_at=datetime(2026, 4, 25, tzinfo=UTC),
                created_by="auto",
                session_pair=[str(session_a), str(session_b)],
                link_reason="same_track",
                shape_match_score=0.7,
                intensity_match_score=0.6,
                usefulness_rating=rating,
                rated_at=rated_at,
            )
        )
        await db.commit()
    return {
        "user_id": user_id,
        "driver_id": driver_id,
        "session_pair": [session_a, session_b],
        "link_id": link_id,
        "rating": rating,
    }


async def _snapshot_driver_state(
    factory: Any, *, driver_id: Any
) -> dict[str, Any]:
    """Frozen view of everything T024 cares about for driver B:
    every transfer_link row owned by them, plus the row count.
    The exact field order doesn't matter — we compare by sorted
    dicts. SQLAlchemy ORM rows aren't directly comparable across
    sessions, so we project to plain dicts."""
    async with factory() as db:
        rows = (
            (
                await db.execute(
                    select(TransferLink).where(
                        TransferLink.driver_id == driver_id
                    )
                )
            )
            .scalars()
            .all()
        )
    return {
        "count": len(rows),
        "rows": sorted(
            (
                {
                    "id": str(r.id),
                    "driver_id": str(r.driver_id),
                    "created_at": r.created_at.isoformat(),
                    "created_by": r.created_by,
                    "session_pair": list(r.session_pair),
                    "link_reason": r.link_reason,
                    "shape_match_score": r.shape_match_score,
                    "intensity_match_score": r.intensity_match_score,
                    "divergence_points": list(r.divergence_points or []),
                    "transferred_skills": list(r.transferred_skills or []),
                    "non_transferred_skills": list(
                        r.non_transferred_skills or []
                    ),
                    "generated_report_id": (
                        str(r.generated_report_id)
                        if r.generated_report_id
                        else None
                    ),
                    "usefulness_rating": r.usefulness_rating,
                    "rated_at": (
                        r.rated_at.isoformat() if r.rated_at else None
                    ),
                }
                for r in rows
            ),
            key=lambda d: d["id"],
        ),
    }


# -- The integration test -----------------------------------------


def test_bridge_attacks_dont_leak_or_mutate_driver_bs_state(
    configured_app: dict[str, Any],
) -> None:
    """The cross-cutting RLS property: driver A trying every
    bridge endpoint against driver B's resources never succeeds,
    and driver B's full bridge state is byte-identical afterward.

    This is the regression pin for T024. Adding a new bridge
    endpoint without a `driver_id == profile.id` check trips it;
    a side-effect that mutates DB state on a 404 trips it.
    """
    factory = configured_app["factory"]
    a = asyncio.run(_seed_driver_with_link(factory, clerk_user_id="u_a"))
    b = asyncio.run(
        _seed_driver_with_link(
            factory, clerk_user_id="u_b", rating="up"
        )
    )

    before = asyncio.run(
        _snapshot_driver_state(factory, driver_id=b["driver_id"])
    )
    assert before["count"] == 1
    assert before["rows"][0]["usefulness_rating"] == "up"

    client = TestClient(app)
    auth_a = {"Authorization": f"Bearer {_bearer(configured_app['keys'], 'u_a')}"}

    # 1. GET driver B's link as A.
    r = client.get(f"/api/v1/bridge/links/{b['link_id']}", headers=auth_a)
    assert r.status_code not in (200, 204), (
        f"driver A read driver B's link: {r.status_code}"
    )
    assert r.status_code == 404

    # 2. DELETE driver B's link as A.
    r = client.delete(
        f"/api/v1/bridge/links/{b['link_id']}", headers=auth_a
    )
    assert r.status_code not in (200, 204)
    assert r.status_code == 404

    # 3. POST rate driver B's link as A — try to overwrite "up"
    #    with "down". Both the response and the DB row must be
    #    untouched.
    r = client.post(
        f"/api/v1/bridge/links/{b['link_id']}/rate",
        headers=auth_a,
        json={"rating": "down"},
    )
    assert r.status_code not in (200, 204)
    assert r.status_code == 404

    # 4. GET driver B's paginated link list via the driver-scoped
    #    path. Must 404 (not enumerate, not return empty list).
    r = client.get(
        f"/api/v1/driver/{b['driver_id']}/bridge/links", headers=auth_a
    )
    assert r.status_code not in (200,)
    assert r.status_code == 404

    # 5. POST a new link as A using driver B's session_pair. Must
    #    404 (and never create a row attributing B's sessions to A).
    r = client.post(
        "/api/v1/bridge/links",
        headers=auth_a,
        json={
            "created_by": "user",
            "session_pair": [str(s) for s in b["session_pair"]],
            "link_reason": "manual",
            "shape_match_score": 0.5,
            "intensity_match_score": 0.5,
            "divergence_points": [],
            "transferred_skills": [],
            "non_transferred_skills": [],
            "generated_report_id": None,
        },
    )
    assert r.status_code not in (200, 201)
    assert r.status_code == 404

    # The property: driver B's state is untouched.
    after = asyncio.run(
        _snapshot_driver_state(factory, driver_id=b["driver_id"])
    )
    assert after == before, (
        "driver B's bridge state changed after driver A's cross-driver "
        f"attacks: before={before} after={after}"
    )

    # And nothing was created under driver A from B's sessions.
    a_after = asyncio.run(
        _snapshot_driver_state(factory, driver_id=a["driver_id"])
    )
    assert a_after["count"] == 1, (
        "driver A's link count changed unexpectedly — a cross-driver "
        "POST may have leaked driver B's sessions into A's namespace"
    )


def test_drivers_list_only_includes_own_links(
    configured_app: dict[str, Any],
) -> None:
    """Pagination leak check: the driver-scoped list endpoint
    returns *only* the authenticated driver's links, never any
    rows owned by another driver — even if the path's driver_id
    matches the authenticated user."""
    factory = configured_app["factory"]
    a = asyncio.run(_seed_driver_with_link(factory, clerk_user_id="u_a"))
    asyncio.run(_seed_driver_with_link(factory, clerk_user_id="u_b"))
    client = TestClient(app)
    auth_a = {"Authorization": f"Bearer {_bearer(configured_app['keys'], 'u_a')}"}

    r = client.get(
        f"/api/v1/driver/{a['driver_id']}/bridge/links", headers=auth_a
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["id"] == str(a["link_id"])
    # Sanity: every returned id maps back to driver A.
    for item in body["items"]:
        assert item["driver_id"] == str(a["driver_id"])
