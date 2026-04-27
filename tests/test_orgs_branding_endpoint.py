"""T175 (sprint2-backend) — org branding metadata.

Adds `organizations.branding_logo_url` (nullable str, ≤2048 chars)
and `PATCH /api/v1/orgs/:id/branding` for admin updates.

Why a `/branding` sub-path rather than a generic PATCH on the
org root? Custom-domain (deferred) and future theme/skin fields
will join branding_logo_url under this namespace. Keeping the
path explicit makes the intent visible and avoids overloading
a generic edit surface (which would invite scope creep onto
plan/seats_limit/admin_user_id — all of which have separate
flows for separate reasons).

Auth: only the org admin can edit branding. Non-admin members
(coach, driver) and outsiders all 403, status parity with
unknown-org. The set of who-can-rebrand-the-org is narrower
than who-can-read-it, matching the existing `/invites` admin
gate doctrine.

Pinned behaviors:
  - 401 unauthenticated
  - 403 caller is non-admin org member (coach)
  - 403 caller not in org
  - 403 unknown org (status parity)
  - 422 empty string body (use null to clear)
  - 422 URL longer than 2048 chars
  - 200 admin sets branding_logo_url
  - 200 admin clears (null) branding_logo_url
  - 200 response carries the updated value (Organization shape)
  - 200 GET /api/v1/orgs/:id/members still works (no migration
    breakage on the orgs table)
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
from sqlalchemy import inspect, select

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
def upgraded_db_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


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


def _hdrs(ca: dict[str, Any], clerk: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_bearer(ca['keys'], clerk)}"}


def _url(org_id: Any) -> str:
    return f"/api/v1/orgs/{org_id}/branding"


async def _seed_org_with_admin_and_coach(
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
            clerk_user_id=f"coach175_{secrets.token_hex(3)}",
            email=f"coach175_{secrets.token_hex(3)}@t",
        )
        db.add_all([admin, coach])
        await db.flush()
        org = Organization(
            id=uuid6.uuid7(),
            name="T175 Org",
            plan="team",
            admin_user_id=admin.id,
            seats_limit=10,
        )
        db.add(org)
        await db.flush()
        for u, role in (
            (admin, "admin"),
            (coach, "coach"),
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
        }


# -- migration ---------------------------------------------------


async def test_organizations_table_has_branding_logo_url_column(
    upgraded_db_url: str,
) -> None:
    """Migration adds the column. Pin: an admin running
    `alembic upgrade head` gets the new column."""
    engine = make_engine(upgraded_db_url)
    try:
        async with engine.connect() as conn:
            cols = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_columns(
                    "organizations"
                )
            )
        names = {c["name"] for c in cols}
        assert "branding_logo_url" in names
    finally:
        await engine.dispose()


async def test_branding_logo_url_is_nullable(
    upgraded_db_url: str,
) -> None:
    """Org-without-branding is the default state. Pin: NOT NULL
    would break create_organization() and existing rows on
    upgrade."""
    engine = make_engine(upgraded_db_url)
    try:
        async with engine.connect() as conn:
            cols = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_columns(
                    "organizations"
                )
            )
        col = next(
            c for c in cols if c["name"] == "branding_logo_url"
        )
        assert col["nullable"] is True
    finally:
        await engine.dispose()


# -- auth + RLS --------------------------------------------------


def test_patch_branding_unauthenticated_returns_401(
    configured_app: dict[str, Any],
) -> None:
    seed = asyncio.run(
        _seed_org_with_admin_and_coach(
            configured_app["factory"], admin_clerk="adm175a"
        )
    )
    with TestClient(app) as client:
        resp = client.patch(
            _url(seed["org_id"]),
            json={"branding_logo_url": "https://cdn.test/logo.png"},
        )
    assert resp.status_code == 401


def test_patch_branding_caller_is_coach_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """Coach role can't edit branding — admin only."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_admin_and_coach(factory, admin_clerk="adm175b")
    )
    with TestClient(app) as client:
        resp = client.patch(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["coach_clerk"]),
            json={"branding_logo_url": "https://cdn.test/logo.png"},
        )
    assert resp.status_code == 403


def test_patch_branding_caller_not_in_org_returns_403(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_admin_and_coach(factory, admin_clerk="adm175c")
    )

    async def _seed_outsider() -> None:
        async with factory() as db:
            db.add(
                User(
                    id=uuid6.uuid7(),
                    clerk_user_id="outsider175",
                    email="outsider175@t",
                )
            )
            await db.commit()
    asyncio.run(_seed_outsider())

    with TestClient(app) as client:
        resp = client.patch(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, "outsider175"),
            json={"branding_logo_url": "https://cdn.test/logo.png"},
        )
    assert resp.status_code == 403


def test_patch_branding_unknown_org_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """Status parity: unknown-org and not-admin both 403."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_admin_and_coach(factory, admin_clerk="adm175d")
    )
    fake_org = uuid6.uuid7()
    with TestClient(app) as client:
        resp = client.patch(
            _url(fake_org),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
            json={"branding_logo_url": "https://cdn.test/logo.png"},
        )
    assert resp.status_code == 403


# -- validation --------------------------------------------------


def test_patch_branding_empty_string_returns_422(
    configured_app: dict[str, Any],
) -> None:
    """Empty string is rejected — use null to clear instead.
    Pin: prevents the ambiguous '' state in the DB column."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_admin_and_coach(factory, admin_clerk="adm175e")
    )
    with TestClient(app) as client:
        resp = client.patch(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
            json={"branding_logo_url": ""},
        )
    assert resp.status_code == 422


def test_patch_branding_too_long_url_returns_422(
    configured_app: dict[str, Any],
) -> None:
    """URL longer than 2048 chars is rejected. Pin: keeps the
    column tractable; 2048 is the conservative HTTP URL ceiling."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_admin_and_coach(factory, admin_clerk="adm175f")
    )
    long_url = "https://cdn.test/" + ("a" * 2050)
    with TestClient(app) as client:
        resp = client.patch(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
            json={"branding_logo_url": long_url},
        )
    assert resp.status_code == 422


# -- happy paths -------------------------------------------------


def test_patch_branding_admin_sets_url_returns_200(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_admin_and_coach(factory, admin_clerk="adm175g")
    )
    with TestClient(app) as client:
        resp = client.patch(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
            json={
                "branding_logo_url": "https://cdn.test/logo-v1.png"
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (
        body["branding_logo_url"] == "https://cdn.test/logo-v1.png"
    )


def test_patch_branding_persists_to_db(
    configured_app: dict[str, Any],
) -> None:
    """The PATCH actually writes to the column. Pin: end-to-end
    write-path."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_admin_and_coach(factory, admin_clerk="adm175h")
    )
    target_url = "https://logos.example.com/team-x.svg"
    with TestClient(app) as client:
        client.patch(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
            json={"branding_logo_url": target_url},
        )

    async def _read_back() -> str | None:
        async with factory() as db:
            org = (
                await db.execute(
                    select(Organization).where(
                        Organization.id == seed["org_id"]
                    )
                )
            ).scalar_one()
            return org.branding_logo_url
    assert asyncio.run(_read_back()) == target_url


def test_patch_branding_admin_clears_url(
    configured_app: dict[str, Any],
) -> None:
    """Submitting null clears the previous value. Pin: distinct
    from omitting the field — null is the explicit clear."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_admin_and_coach(factory, admin_clerk="adm175i")
    )
    with TestClient(app) as client:
        # Set
        client.patch(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
            json={"branding_logo_url": "https://cdn.test/x.png"},
        )
        # Clear
        resp = client.patch(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
            json={"branding_logo_url": None},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["branding_logo_url"] is None
