"""T167 (sprint2-backend) — GET /api/v1/orgs/:id/members RLS.

Org row-level security applied at the application layer:
  - members of the org (admin, coach, driver) can list its
    members
  - non-members get 403 (status parity with unknown-org to
    avoid existence-leak)
  - role-based filtering: admins see all roles; coaches and
    drivers see only role=driver entries (so the team-mate
    view doesn't leak admin/coach identity to drivers)

Invariant: a coach can see drivers (their coaching cohort) but
not other coaches or the admin. The product reasoning: the
coach's job is the driver, so that's what they see; visibility
into peer coaches is admin-only.

Pinned behaviors:
  - 401 unauthenticated
  - 403 non-member caller (status parity with unknown-org)
  - 403 unknown org
  - 200 admin sees all roles (admin + coach + driver)
  - 200 coach sees only drivers (no admin, no other coaches)
  - 200 driver sees only drivers (no admin, no coaches)
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


async def _seed_org_with_full_team(
    factory: Any,
    *,
    admin_clerk: str,
) -> dict[str, Any]:
    """Seeds an org with an admin, a coach, and two drivers.
    Returns dict with org_id and clerk_id-keyed user_ids."""
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
        driver1 = User(
            id=uuid6.uuid7(),
            clerk_user_id=f"d1_{secrets.token_hex(3)}",
            email=f"d1_{secrets.token_hex(3)}@t",
        )
        driver2 = User(
            id=uuid6.uuid7(),
            clerk_user_id=f"d2_{secrets.token_hex(3)}",
            email=f"d2_{secrets.token_hex(3)}@t",
        )
        db.add_all([admin, coach, driver1, driver2])
        await db.flush()
        org = Organization(
            id=uuid6.uuid7(),
            name="T167 Org",
            plan="team",
            admin_user_id=admin.id,
            seats_limit=20,
        )
        db.add(org)
        await db.flush()
        for u, role in (
            (admin, "admin"),
            (coach, "coach"),
            (driver1, "driver"),
            (driver2, "driver"),
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
            "admin_clerk": admin.clerk_user_id,
            "coach_clerk": coach.clerk_user_id,
            "driver1_clerk": driver1.clerk_user_id,
            "driver2_clerk": driver2.clerk_user_id,
            "admin_user_id": admin.id,
            "coach_user_id": coach.id,
            "driver1_user_id": driver1.id,
            "driver2_user_id": driver2.id,
        }


def _hdrs(ca: dict[str, Any], clerk_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_bearer(ca['keys'], clerk_id)}"}


# -- auth --------------------------------------------------------


def test_get_members_unauthenticated_returns_401(
    configured_app: dict[str, Any],
) -> None:
    seed = asyncio.run(
        _seed_org_with_full_team(
            configured_app["factory"], admin_clerk="adm167a"
        )
    )
    with TestClient(app) as client:
        resp = client.get(f"/api/v1/orgs/{seed['org_id']}/members")
    assert resp.status_code == 401


def test_get_members_non_member_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """A user not in the org can't list its members. Pin: prevents
    org-membership enumeration by anyone with a valid JWT."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_full_team(factory, admin_clerk="adm167b")
    )

    async def _seed_outsider() -> None:
        async with factory() as db:
            db.add(
                User(
                    id=uuid6.uuid7(),
                    clerk_user_id="outsider167",
                    email="outsider167@t",
                )
            )
            await db.commit()

    asyncio.run(_seed_outsider())
    with TestClient(app) as client:
        resp = client.get(
            f"/api/v1/orgs/{seed['org_id']}/members",
            headers=_hdrs(configured_app, "outsider167"),
        )
    assert resp.status_code == 403


def test_get_members_unknown_org_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """Status parity with non-member: unknown-org and not-a-member
    return the same response, preventing org-existence leakage."""
    factory = configured_app["factory"]

    async def _seed_caller() -> None:
        async with factory() as db:
            db.add(
                User(
                    id=uuid6.uuid7(),
                    clerk_user_id="ghost167",
                    email="ghost167@t",
                )
            )
            await db.commit()

    asyncio.run(_seed_caller())
    fake_org = uuid6.uuid7()
    with TestClient(app) as client:
        resp = client.get(
            f"/api/v1/orgs/{fake_org}/members",
            headers=_hdrs(configured_app, "ghost167"),
        )
    assert resp.status_code == 403


# -- RLS happy paths --------------------------------------------


def test_get_members_admin_sees_all_roles(
    configured_app: dict[str, Any],
) -> None:
    """Admin gets the full roster: 1 admin + 1 coach + 2 drivers
    = 4 entries, all roles represented."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_full_team(factory, admin_clerk="adm167c")
    )
    with TestClient(app) as client:
        resp = client.get(
            f"/api/v1/orgs/{seed['org_id']}/members",
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )
    assert resp.status_code == 200, resp.text
    entries = resp.json()["entries"]
    roles = sorted(e["role"] for e in entries)
    assert roles == ["admin", "coach", "driver", "driver"]


def test_get_members_coach_sees_only_drivers(
    configured_app: dict[str, Any],
) -> None:
    """Coach gets driver-only filtered roster — no admin, no
    peer coaches. Pin: enforces the RLS doctrine that coaches
    work with drivers, not visibility into peer coaches or
    org-billing-admin."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_full_team(factory, admin_clerk="adm167d")
    )
    with TestClient(app) as client:
        resp = client.get(
            f"/api/v1/orgs/{seed['org_id']}/members",
            headers=_hdrs(configured_app, seed["coach_clerk"]),
        )
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert len(entries) == 2  # 2 drivers, no admin, no coach
    assert all(e["role"] == "driver" for e in entries)


def test_get_members_driver_sees_only_drivers(
    configured_app: dict[str, Any],
) -> None:
    """Driver gets driver-only roster — same RLS as coach. Pin:
    drivers see their cohort peers but not their coach or
    org admin."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_full_team(factory, admin_clerk="adm167e")
    )
    with TestClient(app) as client:
        resp = client.get(
            f"/api/v1/orgs/{seed['org_id']}/members",
            headers=_hdrs(configured_app, seed["driver1_clerk"]),
        )
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert len(entries) == 2
    assert all(e["role"] == "driver" for e in entries)


def test_get_members_response_shape(
    configured_app: dict[str, Any],
) -> None:
    """Each entry carries id, org_id, user_id, role, created_at.
    Pin: response shape is the API contract."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_full_team(factory, admin_clerk="adm167f")
    )
    with TestClient(app) as client:
        resp = client.get(
            f"/api/v1/orgs/{seed['org_id']}/members",
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    sample = entries[0]
    assert set(sample.keys()) == {
        "id", "org_id", "user_id", "role", "created_at",
    }
    assert sample["org_id"] == str(seed["org_id"])
