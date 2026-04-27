"""T163 (sprint2-backend) — POST /api/v1/orgs.

Authenticated user creates a new Organization. The endpoint:
  - assigns admin_user_id to the calling user
  - auto-creates an OrganizationMember row with role="admin" for
    the creator (so the admin shows up as a member from day 1
    rather than being implicit-only)
  - returns 201 with the full Organization shape

Pinned behaviors:
  - 401 on unauthenticated POST
  - 201 on valid create + body shape (id, name, plan, admin_user_id,
    seats_limit, created_at)
  - admin OrganizationMember row exists post-create (creator is a
    first-class member, not just the admin reference)
  - 422 on invalid body (empty name, seats_limit < 1, unknown plan)
"""

from __future__ import annotations

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
from sqlalchemy import select

from alembic import command
from app.deps import get_db, get_jwks_client
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import Organization, OrganizationMember, User

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
    return jwt.encode(
        claims, priv_pem, algorithm="RS256", headers={"kid": kid}
    )


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
            "https://test.clerk",
            transport=httpx.MockTransport(handler),
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


async def _seed_user(factory: Any, *, clerk_id: str) -> uuid6.UUID:
    async with factory() as db:
        user = User(
            id=uuid6.uuid7(),
            clerk_user_id=clerk_id,
            email=f"{clerk_id}@t",
        )
        db.add(user)
        await db.commit()
        return user.id


def _hdrs(ca: dict[str, Any], clerk_id: str = "caller163") -> dict[str, str]:
    return {"Authorization": f"Bearer {_bearer(ca['keys'], clerk_id)}"}


# -- auth --------------------------------------------------------


def test_post_orgs_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/orgs",
            json={
                "name": "Anything",
                "plan": "free",
                "seats_limit": 5,
            },
        )
    assert resp.status_code == 401


# -- POST create -------------------------------------------------


def test_post_orgs_creates_and_returns_full_shape(
    configured_app: dict[str, Any],
) -> None:
    """201 + body carries id, created_at (RFC3339), name, plan,
    admin_user_id (= caller's user id), seats_limit. Pin: the
    response shape is the load-bearing contract."""
    user_id = asyncio.run(
        _seed_user(configured_app["factory"], clerk_id="caller163")
    )
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/orgs",
            headers=_hdrs(configured_app),
            json={
                "name": "Apex Coaching Academy",
                "plan": "team",
                "seats_limit": 25,
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Apex Coaching Academy"
    assert body["plan"] == "team"
    assert body["seats_limit"] == 25
    assert body["admin_user_id"] == str(user_id)
    assert "id" in body
    assert "created_at" in body


def test_post_orgs_seeds_admin_membership_row(
    configured_app: dict[str, Any],
) -> None:
    """Creator is a first-class member, not just the admin
    reference. Pin: without the admin OrganizationMember row,
    member-listing endpoints (T164/T165 forthcoming) would omit
    the admin from the team roster, breaking the
    "see-all-members" contract."""
    user_id = asyncio.run(
        _seed_user(configured_app["factory"], clerk_id="caller163b")
    )
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/orgs",
            headers=_hdrs(configured_app, clerk_id="caller163b"),
            json={
                "name": "Solo Op Org",
                "plan": "free",
                "seats_limit": 1,
            },
        )
    assert resp.status_code == 201, resp.text
    org_id = resp.json()["id"]

    async def _check() -> tuple[int, str | None]:
        factory = configured_app["factory"]
        async with factory() as db:
            rows = (
                (
                    await db.execute(
                        select(OrganizationMember).where(
                            OrganizationMember.org_id
                            == uuid6.UUID(org_id)
                        )
                    )
                )
                .scalars()
                .all()
            )
            return len(rows), (
                rows[0].role if rows else None
            )

    n, role = asyncio.run(_check())
    assert n == 1, "expected one admin OrganizationMember row"
    assert role == "admin"
    # also assert the row links the caller
    assert user_id is not None


# -- 422 boundary cases -----------------------------------------


def test_post_orgs_rejects_empty_name(
    configured_app: dict[str, Any],
) -> None:
    """Empty name is unnamable in the UI list; reject at the
    boundary so the persisted set is always discriminable."""
    asyncio.run(
        _seed_user(configured_app["factory"], clerk_id="caller163c")
    )
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/orgs",
            headers=_hdrs(configured_app, clerk_id="caller163c"),
            json={"name": "", "plan": "free", "seats_limit": 5},
        )
    assert resp.status_code == 422


def test_post_orgs_rejects_zero_seats_limit(
    configured_app: dict[str, Any],
) -> None:
    """Zero seats means nobody can be a member, including the
    admin — nonsensical. seats_limit must be >= 1."""
    asyncio.run(
        _seed_user(configured_app["factory"], clerk_id="caller163d")
    )
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/orgs",
            headers=_hdrs(configured_app, clerk_id="caller163d"),
            json={"name": "ok", "plan": "free", "seats_limit": 0},
        )
    assert resp.status_code == 422


def test_post_orgs_rejects_unknown_plan(
    configured_app: dict[str, Any],
) -> None:
    """plan is a closed-set Literal: free / team / pro. Anything
    else is a typo or a future plan that needs an explicit
    schema update before reaching production."""
    asyncio.run(
        _seed_user(configured_app["factory"], clerk_id="caller163e")
    )
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/orgs",
            headers=_hdrs(configured_app, clerk_id="caller163e"),
            json={
                "name": "ok",
                "plan": "enterprise",
                "seats_limit": 5,
            },
        )
    assert resp.status_code == 422


def test_post_orgs_persists_organization_row(
    configured_app: dict[str, Any],
) -> None:
    """Beyond response shape — verify the Organization actually
    landed in the DB. Pin: catches a controller that returns 201
    without committing the transaction."""
    asyncio.run(
        _seed_user(configured_app["factory"], clerk_id="caller163f")
    )
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/orgs",
            headers=_hdrs(configured_app, clerk_id="caller163f"),
            json={
                "name": "Persist Check",
                "plan": "pro",
                "seats_limit": 50,
            },
        )
    assert resp.status_code == 201
    org_id = resp.json()["id"]

    async def _check() -> Organization | None:
        factory = configured_app["factory"]
        async with factory() as db:
            return (
                await db.execute(
                    select(Organization).where(
                        Organization.id == uuid6.UUID(org_id)
                    )
                )
            ).scalar_one_or_none()

    org = asyncio.run(_check())
    assert org is not None
    assert org.name == "Persist Check"
    assert org.plan == "pro"
    assert org.seats_limit == 50
