"""T176 (sprint2-backend) — DELETE /api/v1/orgs/:id.

Org admin permanently deletes the organization. Cascading
cleanup removes org-scoped rows; personal data is preserved.

Cascade (deleted with the org):
  - OrganizationMember rows for that org_id (FK ondelete=CASCADE
    already in the schema since T162)
  - OrganizationInvitation rows for that org_id (FK ondelete=
    CASCADE since T164)

Preserved (personal/session data, NOT org-owned):
  - User rows — the human still exists, just unlinked from the
    org
  - DriverProfile rows — driving history is the user's, not the
    org's
  - Session rows + downstream session-derived rows
  - CoachSessionNote rows — review history survives (the notes
    don't FK to organizations; they FK only to session + author)

Auth: admin only. Non-admin members (coach, driver) and
outsiders all 403, status parity with unknown-org. The
admin-only gate matches T166 remove-member; consistency
across destructive org operations.

Pinned behaviors:
  - 401 unauthenticated
  - 403 caller is coach in org
  - 403 caller is driver in org
  - 403 caller not in org
  - 403 unknown org (status parity)
  - 204 admin deletes successfully — org row gone
  - 204 cascade deletes member rows for the org
  - 204 cascade deletes invitation rows for the org
  - 204 user rows survive (the human persists)
  - 204 driver profile rows survive
  - 204 session rows survive
  - 204 coach_session_notes survive (review history preserved)
  - 204 OTHER orgs untouched (deletion is scoped to the
    targeted org)
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
from sqlalchemy import func, select

from alembic import command
from app.deps import get_db, get_jwks_client
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import (
    CoachSessionNote,
    DriverProfile,
    Organization,
    OrganizationInvitation,
    OrganizationMember,
    User,
)
from infra.orm import Session as SessionRow

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
    # SQLite enforces FK cascades only with PRAGMA foreign_keys=ON;
    # the migration code path enables it. We pin the same env var
    # so the test's session also has FKs on.
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
    return f"/api/v1/orgs/{org_id}"


async def _seed_full_org(
    factory: Any, *, admin_clerk: str
) -> dict[str, Any]:
    """Seeds: admin + coach + driver, with org membership +
    one pending invite + one DriverProfile + one session + one
    coach_session_note. All the org-scoped and personal data
    types are present so the deletion test can verify what
    cascades vs survives."""
    async with factory() as db:
        admin = User(
            id=uuid6.uuid7(),
            clerk_user_id=admin_clerk,
            email=f"{admin_clerk}@t",
        )
        coach = User(
            id=uuid6.uuid7(),
            clerk_user_id=f"coach176_{secrets.token_hex(3)}",
            email=f"coach176_{secrets.token_hex(3)}@t",
        )
        driver = User(
            id=uuid6.uuid7(),
            clerk_user_id=f"drv176_{secrets.token_hex(3)}",
            email=f"drv176_{secrets.token_hex(3)}@t",
        )
        db.add_all([admin, coach, driver])
        await db.flush()
        org = Organization(
            id=uuid6.uuid7(),
            name="T176 Org",
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
        db.add(
            OrganizationInvitation(
                id=uuid6.uuid7(),
                org_id=org.id,
                email="future@t.test",
                role="coach",
                token=secrets.token_urlsafe(32),
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
        profile = DriverProfile(
            id=uuid6.uuid7(),
            user_id=driver.id,
            display_name="Driver",
            skill_level="intermediate",
            cohort_opt_in=False,
        )
        db.add(profile)
        await db.flush()
        session = SessionRow(
            id=uuid6.uuid7(), driver_id=profile.id, manifest={}
        )
        db.add(session)
        await db.flush()
        db.add(
            CoachSessionNote(
                id=uuid6.uuid7(),
                session_id=session.id,
                author_user_id=coach.id,
                body="watch your apex",
            )
        )
        await db.commit()
        return {
            "org_id": org.id,
            "admin_clerk": admin.clerk_user_id,
            "coach_clerk": coach.clerk_user_id,
            "driver_clerk": driver.clerk_user_id,
            "admin_user_id": admin.id,
            "coach_user_id": coach.id,
            "driver_user_id": driver.id,
            "driver_profile_id": profile.id,
            "session_id": session.id,
        }


# -- auth --------------------------------------------------------


def test_delete_org_unauthenticated_returns_401(
    configured_app: dict[str, Any],
) -> None:
    seed = asyncio.run(
        _seed_full_org(
            configured_app["factory"], admin_clerk="adm176a"
        )
    )
    with TestClient(app) as client:
        resp = client.delete(_url(seed["org_id"]))
    assert resp.status_code == 401


def test_delete_org_caller_is_coach_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """Coach role can't delete the org. Admin only."""
    factory = configured_app["factory"]
    seed = asyncio.run(_seed_full_org(factory, admin_clerk="adm176b"))
    with TestClient(app) as client:
        resp = client.delete(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["coach_clerk"]),
        )
    assert resp.status_code == 403


def test_delete_org_caller_is_driver_returns_403(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    seed = asyncio.run(_seed_full_org(factory, admin_clerk="adm176c"))
    with TestClient(app) as client:
        resp = client.delete(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["driver_clerk"]),
        )
    assert resp.status_code == 403


def test_delete_org_caller_not_in_org_returns_403(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    seed = asyncio.run(_seed_full_org(factory, admin_clerk="adm176d"))

    async def _seed_outsider() -> None:
        async with factory() as db:
            db.add(
                User(
                    id=uuid6.uuid7(),
                    clerk_user_id="outsider176",
                    email="outsider176@t",
                )
            )
            await db.commit()
    asyncio.run(_seed_outsider())

    with TestClient(app) as client:
        resp = client.delete(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, "outsider176"),
        )
    assert resp.status_code == 403


def test_delete_org_unknown_org_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """Status parity: unknown-org and not-admin both 403."""
    factory = configured_app["factory"]
    seed = asyncio.run(_seed_full_org(factory, admin_clerk="adm176e"))
    fake_org = uuid6.uuid7()
    with TestClient(app) as client:
        resp = client.delete(
            _url(fake_org),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )
    assert resp.status_code == 403


# -- happy path: cascade + preservation ---------------------------


def test_delete_org_admin_returns_204(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    seed = asyncio.run(_seed_full_org(factory, admin_clerk="adm176f"))
    with TestClient(app) as client:
        resp = client.delete(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )
    assert resp.status_code == 204


def test_delete_org_removes_org_row(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    seed = asyncio.run(_seed_full_org(factory, admin_clerk="adm176g"))
    with TestClient(app) as client:
        client.delete(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )

    async def _check() -> Organization | None:
        async with factory() as db:
            return (
                await db.execute(
                    select(Organization).where(
                        Organization.id == seed["org_id"]
                    )
                )
            ).scalar_one_or_none()
    assert asyncio.run(_check()) is None


def test_delete_org_cascades_member_rows(
    configured_app: dict[str, Any],
) -> None:
    """Member rows for the deleted org are gone (FK CASCADE).
    Pin: org-scoped membership state must not outlive the org;
    otherwise listing an org that no longer exists returns
    orphaned rows."""
    factory = configured_app["factory"]
    seed = asyncio.run(_seed_full_org(factory, admin_clerk="adm176h"))
    with TestClient(app) as client:
        client.delete(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )

    async def _count() -> int:
        async with factory() as db:
            n = (
                await db.execute(
                    select(func.count(OrganizationMember.id)).where(
                        OrganizationMember.org_id == seed["org_id"]
                    )
                )
            ).scalar_one()
            return int(n)
    assert asyncio.run(_count()) == 0


def test_delete_org_cascades_invitation_rows(
    configured_app: dict[str, Any],
) -> None:
    """Outstanding invitations for the deleted org are gone
    (FK CASCADE since T164). Pin: dangling tokens for a dead
    org would 5xx accept-invite."""
    factory = configured_app["factory"]
    seed = asyncio.run(_seed_full_org(factory, admin_clerk="adm176i"))
    with TestClient(app) as client:
        client.delete(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )

    async def _count() -> int:
        async with factory() as db:
            n = (
                await db.execute(
                    select(
                        func.count(OrganizationInvitation.id)
                    ).where(
                        OrganizationInvitation.org_id == seed["org_id"]
                    )
                )
            ).scalar_one()
            return int(n)
    assert asyncio.run(_count()) == 0


def test_delete_org_preserves_user_rows(
    configured_app: dict[str, Any],
) -> None:
    """The humans persist after their org is deleted. Pin: a
    member of a deleted org doesn't get GDPR-purged just
    because the org went away."""
    factory = configured_app["factory"]
    seed = asyncio.run(_seed_full_org(factory, admin_clerk="adm176j"))
    with TestClient(app) as client:
        client.delete(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )

    async def _users_exist() -> tuple[bool, bool, bool]:
        async with factory() as db:
            admin = (
                await db.execute(
                    select(User).where(User.id == seed["admin_user_id"])
                )
            ).scalar_one_or_none()
            coach = (
                await db.execute(
                    select(User).where(User.id == seed["coach_user_id"])
                )
            ).scalar_one_or_none()
            driver = (
                await db.execute(
                    select(User).where(User.id == seed["driver_user_id"])
                )
            ).scalar_one_or_none()
            return (admin is not None, coach is not None, driver is not None)
    states = asyncio.run(_users_exist())
    assert states == (True, True, True)


def test_delete_org_preserves_driver_profiles(
    configured_app: dict[str, Any],
) -> None:
    """DriverProfile is the user's driving history identity —
    org deletion doesn't touch it. Pin: switching teams or
    losing one team shouldn't reset a driver's progress."""
    factory = configured_app["factory"]
    seed = asyncio.run(_seed_full_org(factory, admin_clerk="adm176k"))
    with TestClient(app) as client:
        client.delete(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )

    async def _profile() -> DriverProfile | None:
        async with factory() as db:
            return (
                await db.execute(
                    select(DriverProfile).where(
                        DriverProfile.id == seed["driver_profile_id"]
                    )
                )
            ).scalar_one_or_none()
    assert asyncio.run(_profile()) is not None


def test_delete_org_preserves_sessions(
    configured_app: dict[str, Any],
) -> None:
    """Session rows survive — they're owned by DriverProfile,
    not by Organization."""
    factory = configured_app["factory"]
    seed = asyncio.run(_seed_full_org(factory, admin_clerk="adm176l"))
    with TestClient(app) as client:
        client.delete(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )

    async def _session() -> SessionRow | None:
        async with factory() as db:
            return (
                await db.execute(
                    select(SessionRow).where(
                        SessionRow.id == seed["session_id"]
                    )
                )
            ).scalar_one_or_none()
    assert asyncio.run(_session()) is not None


def test_delete_org_preserves_coach_session_notes(
    configured_app: dict[str, Any],
) -> None:
    """T173 coach_session_notes don't FK to organizations —
    they FK to session + author. Org deletion doesn't touch
    them. Pin: review history preserved across org churn."""
    factory = configured_app["factory"]
    seed = asyncio.run(_seed_full_org(factory, admin_clerk="adm176m"))
    with TestClient(app) as client:
        client.delete(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )

    async def _notes_count() -> int:
        async with factory() as db:
            n = (
                await db.execute(
                    select(func.count(CoachSessionNote.id)).where(
                        CoachSessionNote.session_id
                        == seed["session_id"]
                    )
                )
            ).scalar_one()
            return int(n)
    assert asyncio.run(_notes_count()) == 1


def test_delete_org_does_not_affect_other_orgs(
    configured_app: dict[str, Any],
) -> None:
    """Deletion is scoped to the targeted org. A separately
    seeded org and its members survive untouched. Pin: prevents
    a fat-finger from affecting unrelated orgs."""
    factory = configured_app["factory"]
    seed = asyncio.run(_seed_full_org(factory, admin_clerk="adm176n"))

    async def _seed_other_org() -> Any:
        async with factory() as db:
            other_admin = User(
                id=uuid6.uuid7(),
                clerk_user_id="other_adm176",
                email="other_adm176@t",
            )
            db.add(other_admin)
            await db.flush()
            other_org = Organization(
                id=uuid6.uuid7(),
                name="Other Org",
                plan="team",
                admin_user_id=other_admin.id,
                seats_limit=5,
            )
            db.add(other_org)
            await db.flush()
            db.add(
                OrganizationMember(
                    id=uuid6.uuid7(),
                    org_id=other_org.id,
                    user_id=other_admin.id,
                    role="admin",
                )
            )
            await db.commit()
            return other_org.id
    other_org_id = asyncio.run(_seed_other_org())

    with TestClient(app) as client:
        client.delete(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )

    async def _other_survives() -> bool:
        async with factory() as db:
            o = (
                await db.execute(
                    select(Organization).where(
                        Organization.id == other_org_id
                    )
                )
            ).scalar_one_or_none()
            return o is not None
    assert asyncio.run(_other_survives()) is True
