"""T169 (sprint2-backend) — GET /api/v1/orgs/:id/drivers/:uid/profile.

Coach (or admin) members of an org can view a driver's profile
aggregate via this endpoint. The endpoint is the org-scoped
analogue of GET /api/v1/driver/profile (T004) — same shape, but
viewed by a coach/admin against a driver in their org rather than
the caller's self-view.

RLS doctrine (parallels T167's list-members):
  - admin and coach in the org can read driver profiles
  - drivers cannot peer-spy on cohort drivers via this endpoint
    (their /api/v1/driver/profile self-view exists for self-data)
  - target must be a `role=driver` member of the org — admin and
    peer-coach profiles aren't visible at all (same coach<->coach
    invariant T167 pins)
  - status parity (403) for unknown org, caller-not-in-org,
    target-not-in-org, and target-not-driver — all surface the
    same response so existence isn't leaked

Pinned behaviors:
  - 401 unauthenticated
  - 403 caller not in org
  - 403 unknown org
  - 403 caller is driver-role
  - 403 target not in org
  - 403 target is admin
  - 403 target is coach
  - 200 admin caller, target driver
  - 200 coach caller, target driver
  - 200 response shape is DriverProfileAggregate
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


async def _seed_org_with_team(
    factory: Any, *, admin_clerk: str
) -> dict[str, Any]:
    """Seeds: admin, coach, driver, and a second-coach in one
    org. Returns clerk_id-keyed user_ids."""
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
        coach2 = User(
            id=uuid6.uuid7(),
            clerk_user_id=f"coach2_{secrets.token_hex(3)}",
            email=f"coach2_{secrets.token_hex(3)}@t",
        )
        driver = User(
            id=uuid6.uuid7(),
            clerk_user_id=f"drv_{secrets.token_hex(3)}",
            email=f"drv_{secrets.token_hex(3)}@t",
        )
        db.add_all([admin, coach, coach2, driver])
        await db.flush()
        org = Organization(
            id=uuid6.uuid7(),
            name="T169 Org",
            plan="team",
            admin_user_id=admin.id,
            seats_limit=20,
        )
        db.add(org)
        await db.flush()
        for u, role in (
            (admin, "admin"),
            (coach, "coach"),
            (coach2, "coach"),
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
            "admin_clerk": admin.clerk_user_id,
            "coach_clerk": coach.clerk_user_id,
            "coach2_clerk": coach2.clerk_user_id,
            "driver_clerk": driver.clerk_user_id,
            "admin_user_id": admin.id,
            "coach_user_id": coach.id,
            "coach2_user_id": coach2.id,
            "driver_user_id": driver.id,
        }


def _hdrs(ca: dict[str, Any], clerk: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_bearer(ca['keys'], clerk)}"}


def _url(org_id: Any, user_id: Any) -> str:
    return f"/api/v1/orgs/{org_id}/drivers/{user_id}/profile"


# -- auth + RLS --------------------------------------------------


def test_get_driver_profile_unauthenticated_returns_401(
    configured_app: dict[str, Any],
) -> None:
    seed = asyncio.run(
        _seed_org_with_team(
            configured_app["factory"], admin_clerk="adm169a"
        )
    )
    with TestClient(app) as client:
        resp = client.get(
            _url(seed["org_id"], seed["driver_user_id"])
        )
    assert resp.status_code == 401


def test_get_driver_profile_caller_not_in_org_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """Non-member with valid JWT can't peek at an org's driver
    profile. Status parity with unknown-org prevents existence
    leak."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_team(factory, admin_clerk="adm169b")
    )

    async def _seed_outsider() -> None:
        async with factory() as db:
            db.add(
                User(
                    id=uuid6.uuid7(),
                    clerk_user_id="outsider169",
                    email="outsider169@t",
                )
            )
            await db.commit()
    asyncio.run(_seed_outsider())

    with TestClient(app) as client:
        resp = client.get(
            _url(seed["org_id"], seed["driver_user_id"]),
            headers=_hdrs(configured_app, "outsider169"),
        )
    assert resp.status_code == 403


def test_get_driver_profile_unknown_org_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """Status parity: unknown org and not-a-member surface the
    same 403."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_team(factory, admin_clerk="adm169c")
    )
    fake_org = uuid6.uuid7()
    with TestClient(app) as client:
        resp = client.get(
            _url(fake_org, seed["driver_user_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )
    assert resp.status_code == 403


def test_get_driver_profile_caller_is_driver_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """Drivers don't peer-spy via this endpoint. They have their
    own /api/v1/driver/profile self-view. Pin: prevents peer-driver
    intel-gathering on cohort sessions."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_team(factory, admin_clerk="adm169d")
    )

    async def _add_second_driver() -> dict[str, Any]:
        async with factory() as db:
            d2 = User(
                id=uuid6.uuid7(),
                clerk_user_id=f"drv2_{secrets.token_hex(3)}",
                email=f"drv2_{secrets.token_hex(3)}@t",
            )
            db.add(d2)
            await db.flush()
            db.add(
                OrganizationMember(
                    id=uuid6.uuid7(),
                    org_id=seed["org_id"],
                    user_id=d2.id,
                    role="driver",
                )
            )
            await db.commit()
            return {"clerk": d2.clerk_user_id, "user_id": d2.id}
    d2 = asyncio.run(_add_second_driver())

    with TestClient(app) as client:
        resp = client.get(
            _url(seed["org_id"], d2["user_id"]),
            headers=_hdrs(configured_app, seed["driver_clerk"]),
        )
    assert resp.status_code == 403


def test_get_driver_profile_target_not_member_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """Status parity: target user with no org membership surfaces
    the same 403 a non-existent target would."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_team(factory, admin_clerk="adm169e")
    )

    async def _seed_outsider() -> uuid6.UUID:
        async with factory() as db:
            u = User(
                id=uuid6.uuid7(),
                clerk_user_id="ghost169",
                email="ghost169@t",
            )
            db.add(u)
            await db.commit()
            return u.id
    target_id = asyncio.run(_seed_outsider())

    with TestClient(app) as client:
        resp = client.get(
            _url(seed["org_id"], target_id),
            headers=_hdrs(configured_app, seed["coach_clerk"]),
        )
    assert resp.status_code == 403


def test_get_driver_profile_target_is_admin_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """Coaches can't view the org admin's profile via this
    endpoint. Pin: target-must-be-driver enforces the doctrine
    that this endpoint serves coach<->driver supervision, not
    admin-billing visibility."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_team(factory, admin_clerk="adm169f")
    )
    with TestClient(app) as client:
        resp = client.get(
            _url(seed["org_id"], seed["admin_user_id"]),
            headers=_hdrs(configured_app, seed["coach_clerk"]),
        )
    assert resp.status_code == 403


def test_get_driver_profile_target_is_coach_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """Peer coaches don't see each other through this endpoint.
    Pin: matches T167's RLS — coach<->coach visibility is
    admin-only."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_team(factory, admin_clerk="adm169g")
    )
    with TestClient(app) as client:
        resp = client.get(
            _url(seed["org_id"], seed["coach2_user_id"]),
            headers=_hdrs(configured_app, seed["coach_clerk"]),
        )
    assert resp.status_code == 403


# -- happy paths --------------------------------------------------


def test_get_driver_profile_admin_views_driver_returns_200(
    configured_app: dict[str, Any],
) -> None:
    """Admin can view any driver in the org. Pin: full org
    oversight."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_team(factory, admin_clerk="adm169h")
    )
    with TestClient(app) as client:
        resp = client.get(
            _url(seed["org_id"], seed["driver_user_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )
    assert resp.status_code == 200, resp.text


def test_get_driver_profile_coach_views_driver_returns_200(
    configured_app: dict[str, Any],
) -> None:
    """Coach can view drivers in the same org. Pin: coaches
    operate on drivers — that's their role."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_team(factory, admin_clerk="adm169i")
    )
    with TestClient(app) as client:
        resp = client.get(
            _url(seed["org_id"], seed["driver_user_id"]),
            headers=_hdrs(configured_app, seed["coach_clerk"]),
        )
    assert resp.status_code == 200, resp.text


def test_get_driver_profile_returns_aggregate_shape(
    configured_app: dict[str, Any],
) -> None:
    """Response is the DriverProfileAggregate shape — same as
    /api/v1/driver/profile. Empty driver (no profile rows yet)
    yields the all-None aggregate. Pin: API contract."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_team(factory, admin_clerk="adm169j")
    )
    with TestClient(app) as client:
        resp = client.get(
            _url(seed["org_id"], seed["driver_user_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) >= {
        "progress", "top_weaknesses", "active_prescriptions",
        "readiness",
    }
    # No driver-profile row was seeded, so aggregate is empty:
    assert body["progress"] is None
    assert body["readiness"] is None
    assert body["top_weaknesses"] == []
    assert body["active_prescriptions"] == []
