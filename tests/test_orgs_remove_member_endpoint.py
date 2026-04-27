"""T166 (sprint2-backend) — DELETE /api/v1/orgs/:id/members/:user_id.

Org admin removes a member from their org. The endpoint:
  - validates the caller is the org admin (403 otherwise)
  - returns 404 if the (org, user) pair has no member row
  - rejects removal of the admin themselves (409 Conflict —
    admin departure goes through a separate "transfer admin"
    or "delete org" flow, not member removal)
  - deletes the OrganizationMember row, returns 204

Invitations are not touched — a removed member could be
re-invited later, and the invitation history is audit material.

Pinned behaviors:
  - 401 unauthenticated
  - 403 non-admin caller
  - 403 unknown org (status parity with non-admin to avoid
    org-existence leakage)
  - 404 member not in this org
  - 409 admin removes themselves
  - 204 on success + member row gone
  - 204 doesn't touch other members of the same org
"""

from __future__ import annotations

import asyncio
import base64
import secrets
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


def _bearer(keys: dict[str, Any], clerk_user_id: str) -> str:
    return jwt.encode(
        {
            "sub": clerk_user_id,
            "email": f"{clerk_user_id}@t",
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


async def _seed_org_with_member(
    factory: Any,
    *,
    admin_clerk: str = "adm166",
    member_email: str = "member166@t.test",
    member_role: str = "driver",
) -> tuple[uuid6.UUID, uuid6.UUID, uuid6.UUID]:
    """Returns (org_id, admin_user_id, member_user_id)."""
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
            name="T166 Org",
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
        member = User(
            id=uuid6.uuid7(),
            clerk_user_id=f"mem166_{secrets.token_hex(4)}",
            email=member_email,
        )
        db.add(member)
        await db.flush()
        db.add(
            OrganizationMember(
                id=uuid6.uuid7(),
                org_id=org.id,
                user_id=member.id,
                role=member_role,
            )
        )
        await db.commit()
        return org.id, admin.id, member.id


def _hdrs(ca: dict[str, Any], clerk_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_bearer(ca['keys'], clerk_id)}"}


# -- auth --------------------------------------------------------


def test_delete_member_unauthenticated_returns_401(
    configured_app: dict[str, Any],
) -> None:
    org_id, _, member_id = asyncio.run(
        _seed_org_with_member(configured_app["factory"])
    )
    with TestClient(app) as client:
        resp = client.delete(
            f"/api/v1/orgs/{org_id}/members/{member_id}"
        )
    assert resp.status_code == 401


def test_delete_member_non_admin_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """A non-admin (e.g., the member themselves, or another driver)
    can't remove anyone. Pin: prevents members from kicking each
    other out without admin oversight."""
    factory = configured_app["factory"]
    org_id, _, member_id = asyncio.run(
        _seed_org_with_member(factory, admin_clerk="adm166a")
    )
    # The member tries to remove themselves
    member_clerk_id = "mem166a"

    async def _seed_caller() -> None:
        async with factory() as db:
            db.add(
                User(
                    id=uuid6.uuid7(),
                    clerk_user_id=member_clerk_id,
                    email=f"{member_clerk_id}@t",
                )
            )
            await db.commit()

    asyncio.run(_seed_caller())
    with TestClient(app) as client:
        resp = client.delete(
            f"/api/v1/orgs/{org_id}/members/{member_id}",
            headers=_hdrs(configured_app, member_clerk_id),
        )
    assert resp.status_code == 403


def test_delete_member_unknown_org_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """Status parity with non-admin to avoid org-existence
    leakage."""
    factory = configured_app["factory"]

    async def _seed_caller() -> None:
        async with factory() as db:
            db.add(
                User(
                    id=uuid6.uuid7(),
                    clerk_user_id="ghost166",
                    email="ghost166@t",
                )
            )
            await db.commit()

    asyncio.run(_seed_caller())
    fake_org = uuid6.uuid7()
    fake_user = uuid6.uuid7()
    with TestClient(app) as client:
        resp = client.delete(
            f"/api/v1/orgs/{fake_org}/members/{fake_user}",
            headers=_hdrs(configured_app, "ghost166"),
        )
    assert resp.status_code == 403


# -- 404 missing member -----------------------------------------


def test_delete_member_missing_returns_404(
    configured_app: dict[str, Any],
) -> None:
    """Caller is the admin of the org, but the target user isn't
    a member. Pin: 404 distinguishes 'not authorized' (403)
    from 'authorized but no such member' (404), giving the admin
    actionable feedback."""
    factory = configured_app["factory"]
    org_id, _, _ = asyncio.run(
        _seed_org_with_member(factory, admin_clerk="adm166b")
    )
    fake_user = uuid6.uuid7()
    with TestClient(app) as client:
        resp = client.delete(
            f"/api/v1/orgs/{org_id}/members/{fake_user}",
            headers=_hdrs(configured_app, "adm166b"),
        )
    assert resp.status_code == 404


# -- 409 admin self-removal -------------------------------------


def test_delete_member_admin_self_returns_409(
    configured_app: dict[str, Any],
) -> None:
    """Admin can't remove themselves via this endpoint — that
    would orphan the org (no admin to invite/remove anyone).
    Admin departure goes through a 'transfer admin' or 'delete
    org' flow that hasn't been built yet. Pin: 409 Conflict
    matches the semantics — the operation conflicts with the
    invariant 'every org has exactly one admin'."""
    factory = configured_app["factory"]
    org_id, admin_id, _ = asyncio.run(
        _seed_org_with_member(factory, admin_clerk="adm166c")
    )
    with TestClient(app) as client:
        resp = client.delete(
            f"/api/v1/orgs/{org_id}/members/{admin_id}",
            headers=_hdrs(configured_app, "adm166c"),
        )
    assert resp.status_code == 409


# -- happy path --------------------------------------------------


def test_delete_member_returns_204_and_removes_row(
    configured_app: dict[str, Any],
) -> None:
    """204 + the OrganizationMember row is gone."""
    factory = configured_app["factory"]
    org_id, _, member_id = asyncio.run(
        _seed_org_with_member(factory, admin_clerk="adm166d")
    )
    with TestClient(app) as client:
        resp = client.delete(
            f"/api/v1/orgs/{org_id}/members/{member_id}",
            headers=_hdrs(configured_app, "adm166d"),
        )
    assert resp.status_code == 204

    async def _check() -> OrganizationMember | None:
        async with factory() as db:
            return (
                await db.execute(
                    select(OrganizationMember).where(
                        OrganizationMember.org_id == org_id,
                        OrganizationMember.user_id == member_id,
                    )
                )
            ).scalar_one_or_none()

    assert asyncio.run(_check()) is None


def test_delete_member_does_not_touch_other_members(
    configured_app: dict[str, Any],
) -> None:
    """Removing one member doesn't ripple. Pin: cascade-blast
    radius check — DELETE on this row must be scoped to
    (org_id, user_id) only."""
    factory = configured_app["factory"]
    org_id, admin_id, member_id = asyncio.run(
        _seed_org_with_member(factory, admin_clerk="adm166e")
    )
    # Add a second member to verify the bystander stays put
    bystander_id_holder: list[uuid6.UUID] = []

    async def _add_bystander() -> None:
        async with factory() as db:
            bystander = User(
                id=uuid6.uuid7(),
                clerk_user_id="bystander166",
                email="bystander166@t",
            )
            db.add(bystander)
            await db.flush()
            db.add(
                OrganizationMember(
                    id=uuid6.uuid7(),
                    org_id=org_id,
                    user_id=bystander.id,
                    role="coach",
                )
            )
            await db.commit()
            bystander_id_holder.append(bystander.id)

    asyncio.run(_add_bystander())
    with TestClient(app) as client:
        resp = client.delete(
            f"/api/v1/orgs/{org_id}/members/{member_id}",
            headers=_hdrs(configured_app, "adm166e"),
        )
    assert resp.status_code == 204

    async def _count() -> int:
        async with factory() as db:
            rows = (
                (
                    await db.execute(
                        select(OrganizationMember).where(
                            OrganizationMember.org_id == org_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            return len(rows)

    # admin + bystander remain (= 2)
    assert asyncio.run(_count()) == 2
    assert admin_id is not None
    assert bystander_id_holder[0] is not None
