"""T173 (sprint2-backend) — POST /api/v1/sessions/:id/coach-notes.

Coach (or admin) attaches a free-text note to a driver's session.
The driver sees these notes in their session detail view; coaches
use them for offline review feedback after watching a stint.

RLS doctrine:
  - caller must share an org with the session's driver, with
    role admin or coach
  - drivers cannot self-attach via this endpoint (it's the
    coach<->driver review feedback channel)
  - status parity 403 across unauthorized cases (unknown
    session, driver-without-org, caller-not-coach-in-driver-org,
    caller-is-driver-of-session) so existence isn't leaked

Pinned behaviors:
  - 401 unauthenticated
  - 403 unknown session_id (status parity)
  - 403 caller is the session's driver (driver-role caller)
  - 403 driver has no org membership at all (no shared org
    means no coach<->driver pairing)
  - 403 caller in a different org than the driver
  - 403 caller in the right org but role=driver
  - 422 empty body
  - 201 admin-in-driver's-org caller — note created
  - 201 coach-in-driver's-org caller — note created
  - 201 persisted shape includes id, session_id,
    author_user_id, body, created_at
  - 201 multiple notes per session — each call creates a new row
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
    OrganizationMember,
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


def _hdrs(ca: dict[str, Any], clerk: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_bearer(ca['keys'], clerk)}"}


def _url(session_id: Any) -> str:
    return f"/api/v1/sessions/{session_id}/coach-notes"


async def _seed_org_with_driver_session(
    factory: Any, *, admin_clerk: str
) -> dict[str, Any]:
    """Seeds: admin, coach, driver (with profile + session) all
    in one org."""
    async with factory() as db:
        admin = User(
            id=uuid6.uuid7(),
            clerk_user_id=admin_clerk,
            email=f"{admin_clerk}@t",
        )
        coach = User(
            id=uuid6.uuid7(),
            clerk_user_id=f"coach173_{secrets.token_hex(3)}",
            email=f"coach173_{secrets.token_hex(3)}@t",
        )
        driver = User(
            id=uuid6.uuid7(),
            clerk_user_id=f"drv173_{secrets.token_hex(3)}",
            email=f"drv173_{secrets.token_hex(3)}@t",
        )
        db.add_all([admin, coach, driver])
        await db.flush()
        org = Organization(
            id=uuid6.uuid7(),
            name="T173 Org",
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
        profile = DriverProfile(
            id=uuid6.uuid7(),
            user_id=driver.id,
            display_name="T173 Driver",
            skill_level="intermediate",
            cohort_opt_in=False,
        )
        db.add(profile)
        await db.flush()
        session = SessionRow(
            id=uuid6.uuid7(),
            driver_id=profile.id,
            manifest={},
        )
        db.add(session)
        await db.commit()
        return {
            "org_id": org.id,
            "admin_clerk": admin.clerk_user_id,
            "coach_clerk": coach.clerk_user_id,
            "driver_clerk": driver.clerk_user_id,
            "driver_user_id": driver.id,
            "session_id": session.id,
        }


# -- auth + RLS --------------------------------------------------


def test_post_note_unauthenticated_returns_401(
    configured_app: dict[str, Any],
) -> None:
    seed = asyncio.run(
        _seed_org_with_driver_session(
            configured_app["factory"], admin_clerk="adm173a"
        )
    )
    with TestClient(app) as client:
        resp = client.post(
            _url(seed["session_id"]), json={"body": "good lap"}
        )
    assert resp.status_code == 401


def test_post_note_unknown_session_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """Status parity: unknown session and unauthorized both 403."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_driver_session(factory, admin_clerk="adm173b")
    )
    fake = uuid6.uuid7()
    with TestClient(app) as client:
        resp = client.post(
            _url(fake),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
            json={"body": "x"},
        )
    assert resp.status_code == 403


def test_post_note_caller_is_driver_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """Drivers can't self-annotate via this endpoint — it's the
    coach<->driver feedback channel."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_driver_session(factory, admin_clerk="adm173c")
    )
    with TestClient(app) as client:
        resp = client.post(
            _url(seed["session_id"]),
            headers=_hdrs(configured_app, seed["driver_clerk"]),
            json={"body": "x"},
        )
    assert resp.status_code == 403


def test_post_note_caller_in_different_org_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """Coach in a DIFFERENT org from the session's driver — no
    shared coach<->driver pairing → 403. Pin: cross-org coaching
    is not allowed."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_driver_session(factory, admin_clerk="adm173d")
    )

    async def _seed_other_org_coach() -> str:
        async with factory() as db:
            other_admin = User(
                id=uuid6.uuid7(),
                clerk_user_id="other_adm173",
                email="other_adm173@t",
            )
            other_coach = User(
                id=uuid6.uuid7(),
                clerk_user_id="other_coach173",
                email="other_coach173@t",
            )
            db.add_all([other_admin, other_coach])
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
                    user_id=other_coach.id,
                    role="coach",
                )
            )
            await db.commit()
            return other_coach.clerk_user_id
    other_coach_clerk = asyncio.run(_seed_other_org_coach())

    with TestClient(app) as client:
        resp = client.post(
            _url(seed["session_id"]),
            headers=_hdrs(configured_app, other_coach_clerk),
            json={"body": "x"},
        )
    assert resp.status_code == 403


def test_post_note_driver_with_no_org_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """Driver who isn't a member of any org — no coach can attach
    notes (no coach<->driver pairing exists). Pin: solo drivers
    aren't subject to coach annotation."""
    factory = configured_app["factory"]

    async def _seed_solo_driver_session() -> dict[str, Any]:
        async with factory() as db:
            admin = User(
                id=uuid6.uuid7(),
                clerk_user_id="adm173e",
                email="adm173e@t",
            )
            solo = User(
                id=uuid6.uuid7(),
                clerk_user_id="solo_drv173",
                email="solo_drv173@t",
            )
            db.add_all([admin, solo])
            await db.flush()
            org = Organization(
                id=uuid6.uuid7(),
                name="173e Org",
                plan="team",
                admin_user_id=admin.id,
                seats_limit=5,
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
            profile = DriverProfile(
                id=uuid6.uuid7(),
                user_id=solo.id,
                display_name="Solo",
                skill_level="beginner",
                cohort_opt_in=False,
            )
            db.add(profile)
            await db.flush()
            session = SessionRow(
                id=uuid6.uuid7(),
                driver_id=profile.id,
                manifest={},
            )
            db.add(session)
            await db.commit()
            return {
                "admin_clerk": admin.clerk_user_id,
                "session_id": session.id,
            }
    s = asyncio.run(_seed_solo_driver_session())
    with TestClient(app) as client:
        resp = client.post(
            _url(s["session_id"]),
            headers=_hdrs(configured_app, s["admin_clerk"]),
            json={"body": "x"},
        )
    assert resp.status_code == 403


# -- validation --------------------------------------------------


def test_post_note_empty_body_returns_422(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_driver_session(factory, admin_clerk="adm173f")
    )
    with TestClient(app) as client:
        resp = client.post(
            _url(seed["session_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
            json={"body": ""},
        )
    assert resp.status_code == 422


# -- happy paths -------------------------------------------------


def test_post_note_admin_returns_201(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_driver_session(factory, admin_clerk="adm173g")
    )
    with TestClient(app) as client:
        resp = client.post(
            _url(seed["session_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
            json={"body": "Looked smooth"},
        )
    assert resp.status_code == 201, resp.text


def test_post_note_coach_returns_201(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_driver_session(factory, admin_clerk="adm173h")
    )
    with TestClient(app) as client:
        resp = client.post(
            _url(seed["session_id"]),
            headers=_hdrs(configured_app, seed["coach_clerk"]),
            json={"body": "Coach feedback"},
        )
    assert resp.status_code == 201, resp.text


def test_post_note_response_shape_and_persistence(
    configured_app: dict[str, Any],
) -> None:
    """Response carries id, session_id, author_user_id, body,
    created_at. Row persists with the same values. Pin: API +
    write-path contract."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_driver_session(factory, admin_clerk="adm173i")
    )
    body_text = "Watch your apex on T4 — late by 1 car length."
    with TestClient(app) as client:
        resp = client.post(
            _url(seed["session_id"]),
            headers=_hdrs(configured_app, seed["coach_clerk"]),
            json={"body": body_text},
        )
    assert resp.status_code == 201
    body = resp.json()
    assert set(body.keys()) == {
        "id", "session_id", "author_user_id", "body", "created_at",
    }
    assert body["session_id"] == str(seed["session_id"])
    assert body["body"] == body_text

    async def _persisted_count() -> int:
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
    assert asyncio.run(_persisted_count()) == 1


def test_post_note_multiple_notes_per_session(
    configured_app: dict[str, Any],
) -> None:
    """Coach can leave several notes on one session. Pin: no
    UNIQUE constraint on (session_id) — multi-note is by design."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_driver_session(factory, admin_clerk="adm173j")
    )
    with TestClient(app) as client:
        for i in range(3):
            resp = client.post(
                _url(seed["session_id"]),
                headers=_hdrs(configured_app, seed["coach_clerk"]),
                json={"body": f"observation {i}"},
            )
            assert resp.status_code == 201

    async def _count() -> int:
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
    assert asyncio.run(_count()) == 3
