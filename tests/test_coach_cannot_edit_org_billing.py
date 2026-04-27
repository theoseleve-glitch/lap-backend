"""T169 (sprint2-backend) — coach role can't edit org billing.

T164's invite endpoint and T166's remove-member endpoint are
admin-only via `org.admin_user_id != user.id` 403. This file
pins the role-specific contract: a user with `role=coach` in
the org gets the same 403 as any non-admin caller.

These tests exist explicitly because T169's deliverable is
"Coach role: can view drivers' profiles BUT NOT edit billing"
— the negative half of that contract is the "BUT NOT". The
existing T164/T166 generic non-admin tests cover users-with-no-
membership; this file covers the in-org coach case to lock the
RBAC line at the role level, not just the membership-existence
level.

Pinned behaviors:
  - coach (member, role=coach) can't POST /orgs/:id/invites (403)
  - coach (member, role=coach) can't DELETE /orgs/:id/members/:uid
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


async def _seed_org_admin_coach_driver(
    factory: Any, *, admin_clerk: str
) -> dict[str, Any]:
    async with factory() as db:
        admin = User(
            id=uuid6.uuid7(),
            clerk_user_id=admin_clerk,
            email=f"{admin_clerk}@t",
        )
        coach = User(
            id=uuid6.uuid7(),
            clerk_user_id=f"coach_{secrets.token_hex(3)}",
            email=f"coach_{secrets.token_hex(3)}@t",
        )
        driver = User(
            id=uuid6.uuid7(),
            clerk_user_id=f"drv_{secrets.token_hex(3)}",
            email=f"drv_{secrets.token_hex(3)}@t",
        )
        db.add_all([admin, coach, driver])
        await db.flush()
        org = Organization(
            id=uuid6.uuid7(),
            name="T169 Billing Org",
            plan="team",
            admin_user_id=admin.id,
            seats_limit=10,
        )
        db.add(org)
        await db.flush()
        for u, role in (
            (admin, "admin"),
            (coach, "coach"),
            (driver, "driver"),
        ):
            db.add(
                OrganizationMember(
                    id=uuid6.uuid7(),
                    org_id=org.id,
                    user_id=u.id,
                    role=role,
                )
            )
        await db.commit()
        return {
            "org_id": org.id,
            "coach_clerk": coach.clerk_user_id,
            "driver_user_id": driver.id,
        }


def _hdrs(ca: dict[str, Any], clerk: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_bearer(ca['keys'], clerk)}"}


def test_coach_cannot_create_invitation_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """Coach in org issuing T164 invite → 403 not_org_admin.
    Pin: coach role can't allocate seats / consume billing
    capacity. RBAC at the role level."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_admin_coach_driver(factory, admin_clerk="adm169x")
    )
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/orgs/{seed['org_id']}/invites",
            headers=_hdrs(configured_app, seed["coach_clerk"]),
            json={"email": "newdriver@t.test", "role": "driver"},
        )
    assert resp.status_code == 403, resp.text


def test_coach_cannot_remove_member_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """Coach in org calling T166 remove-member → 403. Pin:
    coach role can't free seats / trigger Stripe credit. RBAC
    at the role level."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_admin_coach_driver(factory, admin_clerk="adm169y")
    )
    with TestClient(app) as client:
        resp = client.delete(
            f"/api/v1/orgs/{seed['org_id']}/"
            f"members/{seed['driver_user_id']}",
            headers=_hdrs(configured_app, seed["coach_clerk"]),
        )
    assert resp.status_code == 403, resp.text
