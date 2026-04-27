"""T195 (sprint2-backend) — GDPR Article 17 right-to-erasure.

DELETE /api/v1/me cascades the caller's user-derived data:
the User row, their DriverProfile, the Sessions tied to that
profile, and their OrganizationMember rows. Schema-level
cascades (ondelete=CASCADE) handle the same in production with
FK enforcement on; this test pins explicit handler behavior so
SQLite tests (FK PRAGMA off by default) verify the same shape.

Sharp edges:

  - Organization.admin_user_id is intentionally NOT cascade
    (orm.py docstring: orphaning members + billing is worse
    than blocking). The endpoint detects this and returns 409
    so the user transfers admin before erasure.

  - get_current_user AUTO-PROVISIONS on first contact. If a
    test re-uses the same JWT after delete to "verify the user
    is gone," it just recreates them. Verification must go
    through a direct DB query for the original user_id.

  - Cross-user isolation matters legally: deleting user A must
    not touch user B's profile / sessions / org memberships.
    Pinned in test_does_not_delete_other_users_data.
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
from app.error_codes import ErrorCode
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import (
    DriverProfile,
    Organization,
    OrganizationMember,
    Session,
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
            "email": f"{clerk_user_id}@t.io",
            "exp": int(time.time()) + 3600,
        },
        keys["kid"],
    )


async def _seed_user(factory: Any, *, clerk_id: str) -> uuid6.UUID:
    async with factory() as db:
        user = User(
            id=uuid6.uuid7(),
            clerk_user_id=clerk_id,
            email=f"{clerk_id}@t.io",
        )
        db.add(user)
        await db.commit()
        return user.id


async def _seed_profile(
    factory: Any, *, user_id: uuid6.UUID
) -> uuid6.UUID:
    async with factory() as db:
        prof = DriverProfile(
            id=uuid6.uuid7(),
            user_id=user_id,
            display_name="X",
        )
        db.add(prof)
        await db.commit()
        return prof.id


async def _seed_session(
    factory: Any, *, driver_id: uuid6.UUID
) -> uuid6.UUID:
    async with factory() as db:
        s = Session(
            id=uuid6.uuid7(),
            driver_id=driver_id,
            manifest={"track": "spa"},
        )
        db.add(s)
        await db.commit()
        return s.id


async def _seed_org(
    factory: Any, *, admin_user_id: uuid6.UUID
) -> uuid6.UUID:
    async with factory() as db:
        org = Organization(
            id=uuid6.uuid7(),
            name="Test Org",
            plan="free",
            admin_user_id=admin_user_id,
            seats_limit=5,
        )
        db.add(org)
        await db.commit()
        return org.id


async def _seed_membership(
    factory: Any,
    *,
    org_id: uuid6.UUID,
    user_id: uuid6.UUID,
    role: str = "driver",
) -> uuid6.UUID:
    async with factory() as db:
        member = OrganizationMember(
            id=uuid6.uuid7(),
            org_id=org_id,
            user_id=user_id,
            role=role,
        )
        db.add(member)
        await db.commit()
        return member.id


def _hdrs(ca: dict[str, Any], clerk_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_bearer(ca['keys'], clerk_id)}"}


async def _user_exists(
    factory: Any, *, user_id: uuid6.UUID
) -> bool:
    async with factory() as db:
        row = (
            await db.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
        return row is not None


async def _profile_exists(
    factory: Any, *, profile_id: uuid6.UUID
) -> bool:
    async with factory() as db:
        row = (
            await db.execute(
                select(DriverProfile).where(DriverProfile.id == profile_id)
            )
        ).scalar_one_or_none()
        return row is not None


async def _session_exists(
    factory: Any, *, session_id: uuid6.UUID
) -> bool:
    async with factory() as db:
        row = (
            await db.execute(
                select(Session).where(Session.id == session_id)
            )
        ).scalar_one_or_none()
        return row is not None


async def _membership_exists(
    factory: Any, *, member_id: uuid6.UUID
) -> bool:
    async with factory() as db:
        row = (
            await db.execute(
                select(OrganizationMember).where(
                    OrganizationMember.id == member_id
                )
            )
        ).scalar_one_or_none()
        return row is not None


# -- auth --------------------------------------------------------


def test_delete_me_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    with TestClient(app) as client:
        resp = client.delete("/api/v1/me")
    assert resp.status_code == 401


# -- happy path: 204 + cascade pinned ---------------------------


def test_delete_me_returns_204_and_user_row_is_gone(
    configured_app: dict[str, Any],
) -> None:
    """The minimal success case. 204 (No Content) is the REST
    convention for DELETE — no body — and the user row must be
    erased from the database. Verifying via direct DB query
    (not a follow-up authenticated request) sidesteps
    get_current_user's auto-provision behavior, which would
    silently recreate the user from the JWT."""
    factory = configured_app["factory"]
    user_id = asyncio.run(_seed_user(factory, clerk_id="erase195a"))
    with TestClient(app) as client:
        resp = client.delete(
            "/api/v1/me", headers=_hdrs(configured_app, "erase195a")
        )
    assert resp.status_code == 204
    assert resp.content == b"", "204 must have empty body"
    assert not asyncio.run(_user_exists(factory, user_id=user_id)), (
        "user row still present after DELETE /api/v1/me"
    )


def test_delete_me_cascades_driver_profile(
    configured_app: dict[str, Any],
) -> None:
    """driver_profiles.user_id is ON DELETE CASCADE in the
    schema, but SQLite tests don't enforce FK by default. The
    handler must explicitly delete the profile so the cascade
    behavior is observable in test (and in prod the FK still
    enforces it as defense-in-depth)."""
    factory = configured_app["factory"]
    user_id = asyncio.run(_seed_user(factory, clerk_id="erase195b"))
    profile_id = asyncio.run(_seed_profile(factory, user_id=user_id))
    with TestClient(app) as client:
        resp = client.delete(
            "/api/v1/me", headers=_hdrs(configured_app, "erase195b")
        )
    assert resp.status_code == 204
    assert not asyncio.run(_profile_exists(factory, profile_id=profile_id))


def test_delete_me_cascades_sessions(
    configured_app: dict[str, Any],
) -> None:
    """sessions.driver_id → driver_profiles.id ON DELETE
    CASCADE. The handler must drop the user's sessions before
    deleting the profile so the cascade is visible without FK
    enforcement. Telemetry sessions are the bulk of "personal
    data generated through use of the service" — leaving them
    behind would be the loudest GDPR violation."""
    factory = configured_app["factory"]
    user_id = asyncio.run(_seed_user(factory, clerk_id="erase195c"))
    profile_id = asyncio.run(_seed_profile(factory, user_id=user_id))
    session_id = asyncio.run(_seed_session(factory, driver_id=profile_id))
    with TestClient(app) as client:
        resp = client.delete(
            "/api/v1/me", headers=_hdrs(configured_app, "erase195c")
        )
    assert resp.status_code == 204
    assert not asyncio.run(_session_exists(factory, session_id=session_id))


def test_delete_me_cascades_organization_memberships(
    configured_app: dict[str, Any],
) -> None:
    """organization_members.user_id is ON DELETE CASCADE per
    its docstring: 'GDPR right-to-erasure on a user purges
    their org memberships with them.' Test pins the cascade
    actually fires (handler doing explicit deletes since
    SQLite has FK off)."""
    factory = configured_app["factory"]
    member_user_id = asyncio.run(
        _seed_user(factory, clerk_id="erase195d_member")
    )
    admin_user_id = asyncio.run(
        _seed_user(factory, clerk_id="erase195d_admin")
    )
    org_id = asyncio.run(
        _seed_org(factory, admin_user_id=admin_user_id)
    )
    member_row_id = asyncio.run(
        _seed_membership(
            factory,
            org_id=org_id,
            user_id=member_user_id,
            role="driver",
        )
    )

    with TestClient(app) as client:
        resp = client.delete(
            "/api/v1/me",
            headers=_hdrs(configured_app, "erase195d_member"),
        )
    assert resp.status_code == 204
    assert not asyncio.run(
        _membership_exists(factory, member_id=member_row_id)
    ), "membership row leaked past user delete"


# -- safety: blocking + isolation --------------------------------


def test_delete_me_is_blocked_when_caller_admins_an_org(
    configured_app: dict[str, Any],
) -> None:
    """Organization.admin_user_id has NO cascade in the schema
    (orm.py docstring: deleting the admin would orphan members
    + billing). The endpoint must detect this and return 409
    so the user transfers admin first. Accidentally deleting
    the admin would land us in a state where the org is alive
    but unmanageable.

    Verify the user row is STILL there afterwards — the block
    must be transactional, not a partial delete."""
    factory = configured_app["factory"]
    admin_id = asyncio.run(
        _seed_user(factory, clerk_id="erase195e_admin")
    )
    asyncio.run(_seed_org(factory, admin_user_id=admin_id))

    with TestClient(app) as client:
        resp = client.delete(
            "/api/v1/me",
            headers=_hdrs(configured_app, "erase195e_admin"),
        )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body.get("detail") == ErrorCode.CANNOT_DELETE_ORG_ADMIN, (
        f"unexpected detail: {body}"
    )
    # Still present — block must not have partial-deleted.
    assert asyncio.run(_user_exists(factory, user_id=admin_id))


def test_delete_me_does_not_delete_other_users_data(
    configured_app: dict[str, Any],
) -> None:
    """Cross-user isolation. Deleting user A must leave user B's
    profile, sessions, and memberships intact. A regression here
    would be a regulator-reportable mass-erasure incident."""
    factory = configured_app["factory"]
    me_id = asyncio.run(_seed_user(factory, clerk_id="erase195f_me"))
    other_id = asyncio.run(_seed_user(factory, clerk_id="erase195f_other"))
    me_profile = asyncio.run(_seed_profile(factory, user_id=me_id))
    other_profile = asyncio.run(_seed_profile(factory, user_id=other_id))
    asyncio.run(_seed_session(factory, driver_id=me_profile))
    other_session = asyncio.run(
        _seed_session(factory, driver_id=other_profile)
    )

    with TestClient(app) as client:
        resp = client.delete(
            "/api/v1/me",
            headers=_hdrs(configured_app, "erase195f_me"),
        )
    assert resp.status_code == 204

    # Other user's data fully intact.
    assert asyncio.run(_user_exists(factory, user_id=other_id))
    assert asyncio.run(
        _profile_exists(factory, profile_id=other_profile)
    )
    assert asyncio.run(
        _session_exists(factory, session_id=other_session)
    ), "other user's session was wrongfully deleted — CRITICAL"


def test_delete_me_idempotent_user_returning_via_same_jwt_gets_fresh_account(
    configured_app: dict[str, Any],
) -> None:
    """Documenting the lifecycle boundary: after delete, the
    same JWT issued by Clerk still verifies. A *new* request
    with that JWT triggers get_current_user's auto-provision
    branch, creating a fresh User row with a NEW id. This is
    fine — the user erased their account but Clerk still knows
    their identity. We pin the behavior so a future change
    that, say, blacklists deleted clerk_user_ids (a different
    feature) doesn't silently break this implicit contract."""
    factory = configured_app["factory"]
    original_id = asyncio.run(
        _seed_user(factory, clerk_id="erase195g")
    )
    with TestClient(app) as client:
        resp1 = client.delete(
            "/api/v1/me",
            headers=_hdrs(configured_app, "erase195g"),
        )
        assert resp1.status_code == 204
        # Re-use same JWT to call /me — auto-provision recreates.
        resp2 = client.get(
            "/api/v1/me",
            headers=_hdrs(configured_app, "erase195g"),
        )
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["id"] != str(original_id), (
        "re-provisioned user must have a fresh id (the old row "
        "was erased); same id would imply we skipped the delete"
    )
    assert body["clerk_user_id"] == "erase195g"
