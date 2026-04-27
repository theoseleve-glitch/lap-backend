"""T165 (sprint2-backend) — POST /api/v1/orgs/:id/invites/:token/accept.

Authenticated user redeems an invitation token. The endpoint:
  - looks up the OrganizationInvitation by (org_id, token); 404 if
    no match (status parity to avoid token enumeration via timing)
  - rejects expired invitations (410 Gone)
  - rejects already-accepted invitations (409 Conflict)
  - rejects email mismatch — calling user's email must match the
    invitation's email (403)
  - creates an OrganizationMember row with the invitation's role,
    sets accepted_at = now() on the invitation, all in one
    transaction
  - returns 201 + OrganizationMembership shape (id, org_id,
    user_id, role, created_at)

The UNIQUE(org_id, user_id) constraint on organization_members
acts as the race-condition net: two simultaneous accepts of two
different invitations for the same user-in-org would fail the
second one with IntegrityError, surfaced as 409.

Pinned behaviors:
  - 401 unauthenticated
  - 404 unknown org
  - 404 unknown / mismatched token
  - 410 expired token
  - 409 already-accepted token
  - 403 email mismatch
  - 201 + body (id, org_id, user_id, role, created_at) and
    OrganizationMember row created with the right role
  - second-accept of the same token returns 409
"""

from __future__ import annotations

import asyncio
import base64
import secrets
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


def _sign_with_email(
    keys: dict[str, Any], clerk_user_id: str, email: str
) -> str:
    return jwt.encode(
        {
            "sub": clerk_user_id,
            "email": email,
            "exp": int(time.time()) + 3600,
        },
        keys["priv_pem"],
        algorithm="RS256",
        headers={"kid": keys["kid"]},
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


async def _seed_org_with_invite(
    factory: Any,
    *,
    invite_email: str,
    invite_role: str = "coach",
    expires_in_days: int = 7,
    accepted: bool = False,
) -> tuple[uuid6.UUID, uuid6.UUID, str]:
    """Returns (org_id, invitation_id, token)."""
    async with factory() as db:
        admin = User(
            id=uuid6.uuid7(),
            clerk_user_id=f"adm165_{secrets.token_hex(4)}",
            email=f"adm165_{secrets.token_hex(4)}@t",
        )
        db.add(admin)
        await db.flush()
        org = Organization(
            id=uuid6.uuid7(),
            name="T165 Org",
            plan="team",
            admin_user_id=admin.id,
            seats_limit=10,
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
        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        invite = OrganizationInvitation(
            id=uuid6.uuid7(),
            org_id=org.id,
            email=invite_email,
            role=invite_role,
            token=token,
            expires_at=now + timedelta(days=expires_in_days),
            accepted_at=now if accepted else None,
        )
        db.add(invite)
        await db.commit()
        return org.id, invite.id, token


async def _seed_user(
    factory: Any, *, clerk_id: str, email: str
) -> uuid6.UUID:
    async with factory() as db:
        user = User(
            id=uuid6.uuid7(),
            clerk_user_id=clerk_id,
            email=email,
        )
        db.add(user)
        await db.commit()
        return user.id


def _hdrs(
    ca: dict[str, Any], clerk_id: str, email: str
) -> dict[str, str]:
    return {
        "Authorization": (
            f"Bearer {_sign_with_email(ca['keys'], clerk_id, email)}"
        )
    }


# -- auth --------------------------------------------------------


def test_post_accept_unauthenticated_returns_401(
    configured_app: dict[str, Any],
) -> None:
    org_id, _, token = asyncio.run(
        _seed_org_with_invite(
            configured_app["factory"], invite_email="invitee@t.test"
        )
    )
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/orgs/{org_id}/invites/{token}/accept"
        )
    assert resp.status_code == 401


# -- 404 unknown org / token ------------------------------------


def test_post_accept_unknown_org_returns_404(
    configured_app: dict[str, Any],
) -> None:
    asyncio.run(
        _seed_user(
            configured_app["factory"],
            clerk_id="caller165a",
            email="caller165a@t",
        )
    )
    fake_org = uuid6.uuid7()
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/orgs/{fake_org}/invites/foo-token/accept",
            headers=_hdrs(
                configured_app, "caller165a", "caller165a@t"
            ),
        )
    assert resp.status_code == 404


def test_post_accept_unknown_token_returns_404(
    configured_app: dict[str, Any],
) -> None:
    """Token mismatch is 404, not 403, to avoid token-existence
    leak via timing/status diff."""
    org_id, _, _ = asyncio.run(
        _seed_org_with_invite(
            configured_app["factory"], invite_email="real@t.test"
        )
    )
    asyncio.run(
        _seed_user(
            configured_app["factory"],
            clerk_id="caller165b",
            email="real@t.test",
        )
    )
    bogus_token = secrets.token_urlsafe(32)
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/orgs/{org_id}/invites/{bogus_token}/accept",
            headers=_hdrs(configured_app, "caller165b", "real@t.test"),
        )
    assert resp.status_code == 404


# -- 410 expired -------------------------------------------------


def test_post_accept_expired_token_returns_410(
    configured_app: dict[str, Any],
) -> None:
    """Expired invites are gone; 410 Gone signals the resource
    existed but is no longer redeemable. Pin: distinguishes
    expired-but-real from never-existed (404) for clearer client
    UX on stale links from old emails."""
    factory = configured_app["factory"]
    org_id, _, token = asyncio.run(
        _seed_org_with_invite(
            factory,
            invite_email="late@t.test",
            expires_in_days=-1,  # already expired
        )
    )
    asyncio.run(
        _seed_user(
            factory, clerk_id="caller165c", email="late@t.test"
        )
    )
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/orgs/{org_id}/invites/{token}/accept",
            headers=_hdrs(configured_app, "caller165c", "late@t.test"),
        )
    assert resp.status_code == 410


# -- 409 already accepted ---------------------------------------


def test_post_accept_already_accepted_returns_409(
    configured_app: dict[str, Any],
) -> None:
    """Accepted invites can't be re-accepted. Pin: prevents
    double-redeem in a UNIQUE(org_id, user_id) world from
    surfacing as a generic 500 IntegrityError."""
    factory = configured_app["factory"]
    org_id, _, token = asyncio.run(
        _seed_org_with_invite(
            factory,
            invite_email="dup@t.test",
            accepted=True,
        )
    )
    asyncio.run(
        _seed_user(
            factory, clerk_id="caller165d", email="dup@t.test"
        )
    )
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/orgs/{org_id}/invites/{token}/accept",
            headers=_hdrs(configured_app, "caller165d", "dup@t.test"),
        )
    assert resp.status_code == 409


# -- 403 email mismatch -----------------------------------------


def test_post_accept_email_mismatch_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """Calling user's email must match the invitation. Pin:
    prevents token-sharing or accidental redemption by a third
    party who ended up with a leaked link."""
    factory = configured_app["factory"]
    org_id, _, token = asyncio.run(
        _seed_org_with_invite(
            factory, invite_email="intended@t.test"
        )
    )
    asyncio.run(
        _seed_user(
            factory,
            clerk_id="wrong165",
            email="someoneelse@t.test",
        )
    )
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/orgs/{org_id}/invites/{token}/accept",
            headers=_hdrs(
                configured_app, "wrong165", "someoneelse@t.test"
            ),
        )
    assert resp.status_code == 403


# -- happy path --------------------------------------------------


def test_post_accept_creates_member_and_marks_invite_accepted(
    configured_app: dict[str, Any],
) -> None:
    """201 + OrganizationMembership shape, OrganizationMember row
    persisted with the invitation's role, and invitation.accepted_at
    populated."""
    factory = configured_app["factory"]
    org_id, invite_id, token = asyncio.run(
        _seed_org_with_invite(
            factory,
            invite_email="happy@t.test",
            invite_role="driver",
        )
    )
    user_id = asyncio.run(
        _seed_user(
            factory, clerk_id="caller165happy", email="happy@t.test"
        )
    )
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/orgs/{org_id}/invites/{token}/accept",
            headers=_hdrs(
                configured_app, "caller165happy", "happy@t.test"
            ),
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["org_id"] == str(org_id)
    assert body["user_id"] == str(user_id)
    assert body["role"] == "driver"
    assert "id" in body
    assert "created_at" in body

    async def _check() -> tuple[OrganizationMember | None, datetime | None]:
        async with factory() as db:
            mem = (
                await db.execute(
                    select(OrganizationMember).where(
                        OrganizationMember.org_id == org_id,
                        OrganizationMember.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            inv = (
                await db.execute(
                    select(OrganizationInvitation).where(
                        OrganizationInvitation.id == invite_id
                    )
                )
            ).scalar_one_or_none()
            return mem, (inv.accepted_at if inv else None)

    mem, accepted_at = asyncio.run(_check())
    assert mem is not None
    assert mem.role == "driver"
    assert accepted_at is not None


def test_post_accept_second_accept_returns_409(
    configured_app: dict[str, Any],
) -> None:
    """Once accepted, the same token can't be re-redeemed. Pin:
    closes the second-accept race window — the first accept's
    accepted_at trip is the deciding gate."""
    factory = configured_app["factory"]
    org_id, _, token = asyncio.run(
        _seed_org_with_invite(
            factory,
            invite_email="twice@t.test",
        )
    )
    asyncio.run(
        _seed_user(
            factory, clerk_id="caller165twice", email="twice@t.test"
        )
    )
    with TestClient(app) as client:
        first = client.post(
            f"/api/v1/orgs/{org_id}/invites/{token}/accept",
            headers=_hdrs(
                configured_app, "caller165twice", "twice@t.test"
            ),
        )
        assert first.status_code == 201
        second = client.post(
            f"/api/v1/orgs/{org_id}/invites/{token}/accept",
            headers=_hdrs(
                configured_app, "caller165twice", "twice@t.test"
            ),
        )
    assert second.status_code == 409
