"""T172 (sprint2-backend) — POST /api/v1/coach/prescriptions.

Coach (or admin) assigns a prescription manually to a driver
in their org. Distinct from the system-generated path:

  - no `weakness_episode_id` link (coach acts on judgment)
  - `expected_context` / `success_condition` default to empty
    dicts (coach reviews outcomes manually)
  - status starts as "proposed" — driver still has to accept,
    same UX semantic as system-generated

RLS doctrine:
  - caller must be admin or coach in the named org
  - driver must be a driver-role member of the same org
  - drivers can't self-assign via this endpoint (it's coach-
    initiated)
  - status parity 403 across unauthorized cases

Pinned behaviors:
  - 401 unauthenticated
  - 403 caller not in org
  - 403 caller is driver-role
  - 403 driver not in named org
  - 403 driver is admin/coach (not role=driver)
  - 422 missing required fields
  - 201 + ActionPrescription shape on success (admin caller)
  - 201 coach caller works
  - 201 status is "proposed", driver_id resolves to the target
    user's DriverProfile id
  - 201 prescription persists with the title/rationale/priority
    the coach supplied
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
from infra.orm import (
    ActionPrescription,
    DriverProfile,
    Organization,
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


async def _seed_org_with_driver(
    factory: Any, *, admin_clerk: str
) -> dict[str, Any]:
    """Seeds: admin, coach, and one driver-with-DriverProfile."""
    async with factory() as db:
        admin = User(
            id=uuid6.uuid7(),
            clerk_user_id=admin_clerk,
            email=f"{admin_clerk}@t",
        )
        coach = User(
            id=uuid6.uuid7(),
            clerk_user_id=f"coach172_{secrets.token_hex(3)}",
            email=f"coach172_{secrets.token_hex(3)}@t",
        )
        driver = User(
            id=uuid6.uuid7(),
            clerk_user_id=f"drv172_{secrets.token_hex(3)}",
            email=f"drv172_{secrets.token_hex(3)}@t",
        )
        db.add_all([admin, coach, driver])
        await db.flush()
        org = Organization(
            id=uuid6.uuid7(),
            name="T172 Org",
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
            display_name="T172 Driver",
            skill_level="intermediate",
            cohort_opt_in=False,
        )
        db.add(profile)
        await db.commit()
        return {
            "org_id": org.id,
            "admin_clerk": admin.clerk_user_id,
            "admin_user_id": admin.id,
            "coach_clerk": coach.clerk_user_id,
            "coach_user_id": coach.id,
            "driver_clerk": driver.clerk_user_id,
            "driver_user_id": driver.id,
            "driver_profile_id": profile.id,
        }


def _body(seed: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    base = {
        "org_id": str(seed["org_id"]),
        "driver_user_id": str(seed["driver_user_id"]),
        "priority": "p1",
        "title": "Late-apex 6 left at Donington",
        "rationale": (
            "Your trail-braking releases too early on this corner; "
            "20 laps focused on holding brake to apex."
        ),
    }
    base.update(overrides)
    return base


# -- auth --------------------------------------------------------


def test_post_coach_prescription_unauthenticated_returns_401(
    configured_app: dict[str, Any],
) -> None:
    seed = asyncio.run(
        _seed_org_with_driver(
            configured_app["factory"], admin_clerk="adm172a"
        )
    )
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/coach/prescriptions", json=_body(seed)
        )
    assert resp.status_code == 401


def test_post_coach_prescription_caller_not_in_org_returns_403(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_driver(factory, admin_clerk="adm172b")
    )

    async def _outsider() -> None:
        async with factory() as db:
            db.add(
                User(
                    id=uuid6.uuid7(),
                    clerk_user_id="outsider172",
                    email="outsider172@t",
                )
            )
            await db.commit()
    asyncio.run(_outsider())

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/coach/prescriptions",
            headers=_hdrs(configured_app, "outsider172"),
            json=_body(seed),
        )
    assert resp.status_code == 403


def test_post_coach_prescription_caller_is_driver_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """Drivers can't self-assign through this endpoint. Pin: it's
    a coach-initiated path (the whole point is a coach acting on
    a driver). Drivers self-accept via the driver/prescriptions
    accept endpoint."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_driver(factory, admin_clerk="adm172c")
    )
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/coach/prescriptions",
            headers=_hdrs(configured_app, seed["driver_clerk"]),
            json=_body(seed),
        )
    assert resp.status_code == 403


def test_post_coach_prescription_driver_not_in_org_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """Coach can't prescribe to a driver who isn't a member of
    the named org. Pin: cross-org prescribing is not allowed."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_driver(factory, admin_clerk="adm172d")
    )

    async def _other_driver() -> uuid6.UUID:
        async with factory() as db:
            other = User(
                id=uuid6.uuid7(),
                clerk_user_id="other_drv172",
                email="other_drv172@t",
            )
            db.add(other)
            await db.flush()
            db.add(
                DriverProfile(
                    id=uuid6.uuid7(),
                    user_id=other.id,
                    display_name="X",
                    skill_level="beginner",
                    cohort_opt_in=False,
                )
            )
            await db.commit()
            return other.id
    other_id = asyncio.run(_other_driver())

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/coach/prescriptions",
            headers=_hdrs(configured_app, seed["coach_clerk"]),
            json=_body(seed, driver_user_id=str(other_id)),
        )
    assert resp.status_code == 403


def test_post_coach_prescription_target_is_admin_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """Can't prescribe to a non-driver-role member. Pin:
    coach<->driver supervision surface."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_driver(factory, admin_clerk="adm172e")
    )
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/coach/prescriptions",
            headers=_hdrs(configured_app, seed["coach_clerk"]),
            json=_body(seed, driver_user_id=str(seed["admin_user_id"])),
        )
    assert resp.status_code == 403


# -- validation --------------------------------------------------


def test_post_coach_prescription_missing_title_returns_422(
    configured_app: dict[str, Any],
) -> None:
    """Schema enforces non-empty title. Pin: the boundary is
    the schema layer, not the controller."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_driver(factory, admin_clerk="adm172f")
    )
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/coach/prescriptions",
            headers=_hdrs(configured_app, seed["admin_clerk"]),
            json=_body(seed, title=""),
        )
    assert resp.status_code == 422


def test_post_coach_prescription_invalid_priority_returns_422(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_driver(factory, admin_clerk="adm172g")
    )
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/coach/prescriptions",
            headers=_hdrs(configured_app, seed["admin_clerk"]),
            json=_body(seed, priority="urgent"),
        )
    assert resp.status_code == 422


# -- happy paths -------------------------------------------------


def test_post_coach_prescription_admin_caller_returns_201(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_driver(factory, admin_clerk="adm172h")
    )
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/coach/prescriptions",
            headers=_hdrs(configured_app, seed["admin_clerk"]),
            json=_body(seed),
        )
    assert resp.status_code == 201, resp.text


def test_post_coach_prescription_coach_caller_returns_201(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_driver(factory, admin_clerk="adm172i")
    )
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/coach/prescriptions",
            headers=_hdrs(configured_app, seed["coach_clerk"]),
            json=_body(seed),
        )
    assert resp.status_code == 201, resp.text


def test_post_coach_prescription_persists_with_correct_fields(
    configured_app: dict[str, Any],
) -> None:
    """The created row carries the coach's title/rationale/
    priority and starts in 'proposed' status (driver still has
    to accept). driver_id resolves to the target's DriverProfile
    id, NOT the user_id. Pin: data wiring."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_driver(factory, admin_clerk="adm172j")
    )
    title = "Throttle smoothness — turn 3 exit"
    rationale = "T3 exit: pickup spike at 30%; smooth to 80% over 1.0s."
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/coach/prescriptions",
            headers=_hdrs(configured_app, seed["coach_clerk"]),
            json=_body(
                seed,
                title=title,
                rationale=rationale,
                priority="p0",
            ),
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["title"] == title
    assert body["rationale"] == rationale
    assert body["priority"] == "p0"
    assert body["status"] == "proposed"
    assert body["driver_id"] == str(seed["driver_profile_id"])
    # No weakness episode link for a coach-assigned drill
    assert body["weakness_episode_id"] is None

    async def _verify_persisted() -> ActionPrescription:
        async with factory() as db:
            return (
                await db.execute(
                    select(ActionPrescription).where(
                        ActionPrescription.id == uuid6.UUID(body["id"])
                    )
                )
            ).scalar_one()
    row = asyncio.run(_verify_persisted())
    assert row.title == title
    assert row.priority == "p0"
    assert row.status == "proposed"
    assert row.driver_id == seed["driver_profile_id"]


def test_post_coach_prescription_drill_template_id_optional(
    configured_app: dict[str, Any],
) -> None:
    """drill_template_id is optional — coaches frequently invent
    custom drills outside the catalog."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_driver(factory, admin_clerk="adm172k")
    )
    with TestClient(app) as client:
        resp_no_drill = client.post(
            "/api/v1/coach/prescriptions",
            headers=_hdrs(configured_app, seed["coach_clerk"]),
            json=_body(seed),
        )
        resp_with_drill = client.post(
            "/api/v1/coach/prescriptions",
            headers=_hdrs(configured_app, seed["coach_clerk"]),
            json=_body(
                seed,
                title="Drill 2",
                drill_template_id="catalog.trail_brake.v2",
            ),
        )
    assert resp_no_drill.status_code == 201
    assert resp_with_drill.status_code == 201
    assert resp_no_drill.json()["drill_template_id"] is None
    assert resp_with_drill.json()["drill_template_id"] == (
        "catalog.trail_brake.v2"
    )
