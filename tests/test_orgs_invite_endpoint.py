"""T164 (sprint2-backend) — POST /api/v1/orgs/:id/invites.

Org admin invites a user by email + role (coach or driver). The
endpoint:
  - validates the caller is the org admin (403 otherwise)
  - validates the email isn't already a member (409 conflict)
  - validates seats_limit hasn't been exhausted by members +
    pending invites (409 conflict)
  - persists an OrganizationInvitation row with a random
    token (secrets.token_urlsafe) and 7-day TTL
  - returns 201 with the full invitation shape including the
    token (T165 redeems by token)

Invite role is restricted to {coach, driver} at the schema
layer — admin transfer is a separate concern out of scope here.

Pinned behaviors:
  - 401 unauthenticated
  - 403 if caller isn't the admin of the org
  - 404 if the org doesn't exist (use 403 for non-admin to
    avoid leaking org existence to non-members; non-existent
    orgs surface as 404 to the admin path only after the
    admin check would have rejected — net: non-members and
    non-existent both look indistinguishable to the caller)
  - 201 + body (id, org_id, email, role, token, created_at,
    expires_at)
  - 422 invalid email format
  - 422 invalid role
  - 409 already a member
  - 409 seats exhausted (members + pending invites >= seats_limit)
  - persists row to DB
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
from infra.orm import (
    Organization,
    OrganizationInvitation,
    OrganizationMember,
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


async def _seed_admin_and_org(
    factory: Any,
    *,
    admin_clerk: str = "admin164",
    seats_limit: int = 10,
) -> tuple[uuid6.UUID, uuid6.UUID]:
    async with factory() as db:
        admin = User(
            id=uuid6.uuid7(),
            clerk_user_id=admin_clerk,
            email=f"{admin_clerk}@t",
        )
        db.add(admin)
        await db.flush()
        org = Organization(
            id=uuid6.uuid7(),
            name="T164 Org",
            plan="team",
            admin_user_id=admin.id,
            seats_limit=seats_limit,
        )
        db.add(org)
        await db.flush()
        db.add(
            OrganizationMember(
                id=uuid6.uuid7(),
                org_id=org.id,
                user_id=admin.id,
                role="admin",
            )
        )
        await db.commit()
        return admin.id, org.id


def _hdrs(ca: dict[str, Any], clerk_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_bearer(ca['keys'], clerk_id)}"}


# -- auth --------------------------------------------------------


def test_post_invite_unauthenticated_returns_401(
    configured_app: dict[str, Any],
) -> None:
    org_id = asyncio.run(_seed_admin_and_org(configured_app["factory"]))[1]
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/orgs/{org_id}/invites",
            json={"email": "newcoach@t.test", "role": "coach"},
        )
    assert resp.status_code == 401


def test_post_invite_non_admin_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """A non-admin caller can't invite to an org. Pin: prevents
    coaches/drivers from issuing invites that consume the org's
    seats budget without admin consent."""
    _, org_id = asyncio.run(_seed_admin_and_org(configured_app["factory"]))
    # Seed a non-admin user
    factory = configured_app["factory"]

    async def _seed() -> None:
        async with factory() as db:
            db.add(
                User(
                    id=uuid6.uuid7(),
                    clerk_user_id="not_admin_164",
                    email="not_admin@t.test",
                )
            )
            await db.commit()

    asyncio.run(_seed())
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/orgs/{org_id}/invites",
            headers=_hdrs(configured_app, "not_admin_164"),
            json={"email": "x@t.test", "role": "coach"},
        )
    assert resp.status_code == 403


def test_post_invite_unknown_org_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """Inviting into an org that doesn't exist surfaces as 403 to
    avoid leaking org-existence to non-members. Status parity
    with the non-admin path."""
    factory = configured_app["factory"]

    async def _seed() -> None:
        async with factory() as db:
            db.add(
                User(
                    id=uuid6.uuid7(),
                    clerk_user_id="ghost_caller_164",
                    email="ghost@t.test",
                )
            )
            await db.commit()

    asyncio.run(_seed())
    fake_org = uuid6.uuid7()
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/orgs/{fake_org}/invites",
            headers=_hdrs(configured_app, "ghost_caller_164"),
            json={"email": "x@t.test", "role": "coach"},
        )
    assert resp.status_code == 403


# -- happy path --------------------------------------------------


def test_post_invite_creates_and_returns_full_shape(
    configured_app: dict[str, Any],
) -> None:
    """201 + body carries id, org_id, email, role, token (opaque
    secret), created_at (RFC3339), expires_at (RFC3339, ~7 days
    from now). Pin: response shape is the contract."""
    _, org_id = asyncio.run(_seed_admin_and_org(configured_app["factory"]))
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/orgs/{org_id}/invites",
            headers=_hdrs(configured_app, "admin164"),
            json={"email": "newcoach@t.test", "role": "coach"},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["org_id"] == str(org_id)
    assert body["email"] == "newcoach@t.test"
    assert body["role"] == "coach"
    assert isinstance(body.get("token"), str) and len(body["token"]) >= 32
    assert "id" in body
    assert "created_at" in body
    assert "expires_at" in body


def test_post_invite_persists_row(
    configured_app: dict[str, Any],
) -> None:
    """Beyond response shape — verify the OrganizationInvitation
    actually landed in the DB. Pin: catches a controller that
    returns 201 without committing."""
    _, org_id = asyncio.run(
        _seed_admin_and_org(
            configured_app["factory"], admin_clerk="admin164b"
        )
    )
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/orgs/{org_id}/invites",
            headers=_hdrs(configured_app, "admin164b"),
            json={"email": "persist@t.test", "role": "driver"},
        )
    assert resp.status_code == 201
    invite_id = resp.json()["id"]

    async def _check() -> OrganizationInvitation | None:
        factory = configured_app["factory"]
        async with factory() as db:
            return (
                await db.execute(
                    select(OrganizationInvitation).where(
                        OrganizationInvitation.id
                        == uuid6.UUID(invite_id)
                    )
                )
            ).scalar_one_or_none()

    inv = asyncio.run(_check())
    assert inv is not None
    assert inv.email == "persist@t.test"
    assert inv.role == "driver"
    assert inv.accepted_at is None


# -- 422 boundary cases -----------------------------------------


def test_post_invite_rejects_invalid_email(
    configured_app: dict[str, Any],
) -> None:
    """Pydantic EmailStr rejects malformed addresses."""
    _, org_id = asyncio.run(
        _seed_admin_and_org(
            configured_app["factory"], admin_clerk="admin164c"
        )
    )
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/orgs/{org_id}/invites",
            headers=_hdrs(configured_app, "admin164c"),
            json={"email": "not-an-email", "role": "coach"},
        )
    assert resp.status_code == 422


def test_post_invite_rejects_admin_role(
    configured_app: dict[str, Any],
) -> None:
    """Admin transfer is a separate concern; invites are limited
    to coach/driver. Pin: lets future admin-transfer endpoint
    take over without retrofitting an inviting flow that already
    half-implements it."""
    _, org_id = asyncio.run(
        _seed_admin_and_org(
            configured_app["factory"], admin_clerk="admin164d"
        )
    )
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/orgs/{org_id}/invites",
            headers=_hdrs(configured_app, "admin164d"),
            json={"email": "x@t.test", "role": "admin"},
        )
    assert resp.status_code == 422


# -- 409 conflict cases -----------------------------------------


def test_post_invite_already_member_returns_409(
    configured_app: dict[str, Any],
) -> None:
    """Inviting an existing member is a state conflict — they're
    already in the org under their current role; re-roling needs
    a different endpoint. Pin: prevents duplicate-invite spam
    against established members."""
    factory = configured_app["factory"]
    _, org_id = asyncio.run(
        _seed_admin_and_org(factory, admin_clerk="admin164e")
    )

    async def _add_member() -> None:
        async with factory() as db:
            existing = User(
                id=uuid6.uuid7(),
                clerk_user_id="existing164e",
                email="existing@t.test",
            )
            db.add(existing)
            await db.flush()
            db.add(
                OrganizationMember(
                    id=uuid6.uuid7(),
                    org_id=org_id,
                    user_id=existing.id,
                    role="driver",
                )
            )
            await db.commit()

    asyncio.run(_add_member())
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/orgs/{org_id}/invites",
            headers=_hdrs(configured_app, "admin164e"),
            json={"email": "existing@t.test", "role": "coach"},
        )
    assert resp.status_code == 409


def test_post_invite_seats_exhausted_returns_409(
    configured_app: dict[str, Any],
) -> None:
    """seats_limit caps members + pending invites. Pin: enforces
    the org's plan tier at the boundary; without it, an admin
    could over-invite and silently create more invitations than
    the seat budget allows, then have a chaotic accept/reject
    queue when seats fill in arrival order."""
    factory = configured_app["factory"]
    # seats_limit=2: admin + 1 pending = 2 → next invite rejected
    _, org_id = asyncio.run(
        _seed_admin_and_org(
            factory, admin_clerk="admin164f", seats_limit=2
        )
    )
    with TestClient(app) as client:
        # First invite consumes the 2nd seat (admin = seat 1)
        resp1 = client.post(
            f"/api/v1/orgs/{org_id}/invites",
            headers=_hdrs(configured_app, "admin164f"),
            json={"email": "first@t.test", "role": "coach"},
        )
        assert resp1.status_code == 201
        # Second invite would push to 3, exceeding the 2-seat cap
        resp2 = client.post(
            f"/api/v1/orgs/{org_id}/invites",
            headers=_hdrs(configured_app, "admin164f"),
            json={"email": "second@t.test", "role": "driver"},
        )
    assert resp2.status_code == 409
