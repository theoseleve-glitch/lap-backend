"""T171 (sprint2-backend) — GET /api/v1/orgs/:id/dashboard.

Coach (or admin) lists per-driver progress entries for an org.
Each entry is a lean "row in a coach's dashboard table":
identity + latest progress + open work counts.

RLS doctrine (parallels T169):
  - admin and coach in the org list all org drivers
  - drivers don't peer-spy via this endpoint
  - status parity 403 for unknown-org / not-member / driver
    caller (no org-existence leak)
  - non-driver members (admin, coach) are NOT in the entries
    (the dashboard is the coach<->driver supervision surface)

Pinned behaviors:
  - 401 unauthenticated
  - 403 caller not in org
  - 403 unknown org
  - 403 caller is driver-role
  - 200 admin lists all drivers
  - 200 coach lists all drivers
  - 200 entries shape (user_id, display_name, skill_level,
    overall_score, last_progress_at, open_weaknesses,
    open_prescriptions)
  - 200 entry values reflect the latest snapshot (when
    multiple progress snapshots exist, the latest wins)
  - 200 driver-without-DriverProfile still appears with
    nullable identity fields and zero counts
  - 200 only `role=driver` org members are in entries
  - 200 open_weaknesses counts only open/improving status;
    resolved/dormant are excluded
  - 200 open_prescriptions counts only proposed/accepted/
    in_progress; completed/abandoned are excluded
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

from alembic import command
from app.deps import get_db, get_jwks_client
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import (
    ActionPrescription,
    DriverProfile,
    DriverProgressSnapshot,
    Organization,
    OrganizationMember,
    User,
    WeaknessEpisode,
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


def _url(org_id: Any) -> str:
    return f"/api/v1/orgs/{org_id}/dashboard"


async def _seed_org_with_team(
    factory: Any, *, admin_clerk: str
) -> dict[str, Any]:
    """Seeds: admin, coach, two drivers (driver1 has a profile,
    driver2 does not — covers the no-profile-yet edge case)."""
    async with factory() as db:
        admin = User(
            id=uuid6.uuid7(),
            clerk_user_id=admin_clerk,
            email=f"{admin_clerk}@t",
        )
        coach = User(
            id=uuid6.uuid7(),
            clerk_user_id=f"coach171_{secrets.token_hex(3)}",
            email=f"coach171_{secrets.token_hex(3)}@t",
        )
        d1 = User(
            id=uuid6.uuid7(),
            clerk_user_id=f"d1_171_{secrets.token_hex(3)}",
            email=f"d1_171_{secrets.token_hex(3)}@t",
        )
        d2 = User(
            id=uuid6.uuid7(),
            clerk_user_id=f"d2_171_{secrets.token_hex(3)}",
            email=f"d2_171_{secrets.token_hex(3)}@t",
        )
        db.add_all([admin, coach, d1, d2])
        await db.flush()
        org = Organization(
            id=uuid6.uuid7(),
            name="T171 Org",
            plan="team",
            admin_user_id=admin.id,
            seats_limit=20,
        )
        db.add(org)
        await db.flush()
        for u, role in (
            (admin, "admin"),
            (coach, "coach"),
            (d1, "driver"),
            (d2, "driver"),
        ):
            db.add(
                OrganizationMember(
                    id=uuid6.uuid7(),
                    org_id=org.id,
                    user_id=u.id,
                    role=role,
                )
            )

        # Driver 1 has a DriverProfile + 2 progress snapshots
        # (latest wins) + 2 weakness episodes (1 open, 1 resolved)
        # + 2 prescriptions (1 accepted, 1 completed).
        profile = DriverProfile(
            id=uuid6.uuid7(),
            user_id=d1.id,
            display_name="Driver One",
            skill_level="intermediate",
            cohort_opt_in=False,
        )
        db.add(profile)
        await db.flush()
        now = datetime.now(UTC)
        old_snap = DriverProgressSnapshot(
            id=uuid6.uuid7(),
            driver_id=profile.id,
            captured_at=now - timedelta(days=2),
            window="trailing_30d",
            window_start=now - timedelta(days=32),
            window_end=now - timedelta(days=2),
            braking={}, throttle_pickup={}, minimum_speed={},
            line_consistency={}, tire_management={}, racecraft={},
            fatigue_consistency={}, strategy_execution={},
            setup_sensitivity={}, adaptation={},
            overall_score=70.0,
            overall_confidence=0.7,
            percentile_vs_cohort=50.0,
            generated_by="test",
        )
        new_snap = DriverProgressSnapshot(
            id=uuid6.uuid7(),
            driver_id=profile.id,
            captured_at=now,
            window="trailing_30d",
            window_start=now - timedelta(days=30),
            window_end=now,
            braking={}, throttle_pickup={}, minimum_speed={},
            line_consistency={}, tire_management={}, racecraft={},
            fatigue_consistency={}, strategy_execution={},
            setup_sensitivity={}, adaptation={},
            overall_score=82.5,
            overall_confidence=0.8,
            percentile_vs_cohort=75.0,
            generated_by="test",
        )
        db.add_all([old_snap, new_snap])
        # Open weakness — counted
        db.add(
            WeaknessEpisode(
                id=uuid6.uuid7(),
                driver_id=profile.id,
                category="cs",
                subcategory="trail_braking",
                first_observed_at=now - timedelta(days=10),
                last_observed_at=now - timedelta(days=1),
                recurrence_count=3,
                severity="medium",
                confidence=0.7,
                status="open",
                library_rule_id="cs.trail_braking.basic",
            )
        )
        # Resolved weakness — NOT counted
        db.add(
            WeaknessEpisode(
                id=uuid6.uuid7(),
                driver_id=profile.id,
                category="cs",
                subcategory="apex_speed",
                first_observed_at=now - timedelta(days=20),
                last_observed_at=now - timedelta(days=15),
                recurrence_count=1,
                severity="low",
                confidence=0.9,
                status="resolved",
                library_rule_id="cs.apex_speed.basic",
                resolved_at=now - timedelta(days=14),
            )
        )
        # Accepted prescription — counted
        db.add(
            ActionPrescription(
                id=uuid6.uuid7(),
                driver_id=profile.id,
                created_at=now - timedelta(days=2),
                priority="med",
                title="Drill A",
                rationale="x",
                expected_context={},
                success_condition={},
                status="accepted",
            )
        )
        # Completed prescription — NOT counted
        db.add(
            ActionPrescription(
                id=uuid6.uuid7(),
                driver_id=profile.id,
                created_at=now - timedelta(days=5),
                priority="low",
                title="Drill B",
                rationale="y",
                expected_context={},
                success_condition={},
                status="completed",
            )
        )

        await db.commit()
        return {
            "org_id": org.id,
            "admin_clerk": admin.clerk_user_id,
            "coach_clerk": coach.clerk_user_id,
            "d1_clerk": d1.clerk_user_id,
            "d1_user_id": d1.id,
            "d2_user_id": d2.id,
            "admin_user_id": admin.id,
            "coach_user_id": coach.id,
        }


# -- auth + RLS --------------------------------------------------


def test_get_dashboard_unauthenticated_returns_401(
    configured_app: dict[str, Any],
) -> None:
    seed = asyncio.run(
        _seed_org_with_team(
            configured_app["factory"], admin_clerk="adm171a"
        )
    )
    with TestClient(app) as client:
        resp = client.get(_url(seed["org_id"]))
    assert resp.status_code == 401


def test_get_dashboard_caller_not_in_org_returns_403(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_team(factory, admin_clerk="adm171b")
    )

    async def _seed_outsider() -> None:
        async with factory() as db:
            db.add(
                User(
                    id=uuid6.uuid7(),
                    clerk_user_id="outsider171",
                    email="outsider171@t",
                )
            )
            await db.commit()
    asyncio.run(_seed_outsider())

    with TestClient(app) as client:
        resp = client.get(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, "outsider171"),
        )
    assert resp.status_code == 403


def test_get_dashboard_unknown_org_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """Status parity: unknown-org and not-member surface as
    the same 403."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_team(factory, admin_clerk="adm171c")
    )
    fake_org = uuid6.uuid7()
    with TestClient(app) as client:
        resp = client.get(
            _url(fake_org),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )
    assert resp.status_code == 403


def test_get_dashboard_caller_is_driver_returns_403(
    configured_app: dict[str, Any],
) -> None:
    """Drivers don't get the coach-roster view via this endpoint.
    Pin: drivers see their own /api/v1/driver/profile self-view."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_team(factory, admin_clerk="adm171d")
    )
    with TestClient(app) as client:
        resp = client.get(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["d1_clerk"]),
        )
    assert resp.status_code == 403


# -- happy paths --------------------------------------------------


def test_get_dashboard_admin_lists_all_drivers(
    configured_app: dict[str, Any],
) -> None:
    """Admin gets one entry per driver-role member of the org.
    Pin: full-org oversight."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_team(factory, admin_clerk="adm171e")
    )
    with TestClient(app) as client:
        resp = client.get(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )
    assert resp.status_code == 200, resp.text
    entries = resp.json()["entries"]
    user_ids = {e["user_id"] for e in entries}
    assert user_ids == {
        str(seed["d1_user_id"]),
        str(seed["d2_user_id"]),
    }


def test_get_dashboard_coach_lists_all_drivers(
    configured_app: dict[str, Any],
) -> None:
    """Coach gets the same per-driver list as admin. Pin: same
    coach<->driver supervision surface, role-symmetric for
    admin/coach."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_team(factory, admin_clerk="adm171f")
    )
    with TestClient(app) as client:
        resp = client.get(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["coach_clerk"]),
        )
    assert resp.status_code == 200, resp.text
    entries = resp.json()["entries"]
    assert len(entries) == 2


def test_get_dashboard_excludes_non_driver_members(
    configured_app: dict[str, Any],
) -> None:
    """Admin and coach members are NOT in the entries list. Pin:
    the dashboard is for drivers — billing-admin and peer-coaches
    visibility is admin-list-only via T167."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_team(factory, admin_clerk="adm171g")
    )
    with TestClient(app) as client:
        resp = client.get(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )
    entries = resp.json()["entries"]
    user_ids = {e["user_id"] for e in entries}
    assert str(seed["admin_user_id"]) not in user_ids
    assert str(seed["coach_user_id"]) not in user_ids


def test_get_dashboard_entry_shape(
    configured_app: dict[str, Any],
) -> None:
    """Entry carries the expected fields. Pin: API contract."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_team(factory, admin_clerk="adm171h")
    )
    with TestClient(app) as client:
        resp = client.get(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )
    entries = resp.json()["entries"]
    assert len(entries) > 0
    sample = entries[0]
    assert set(sample.keys()) == {
        "user_id", "display_name", "skill_level",
        "overall_score", "last_progress_at",
        "open_weaknesses", "open_prescriptions",
    }


def test_get_dashboard_driver_with_profile_has_populated_fields(
    configured_app: dict[str, Any],
) -> None:
    """Driver with profile + snapshots + open weakness/rx —
    entry reflects the latest snapshot's overall_score, open
    counts only count active statuses. Pin: data wiring."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_team(factory, admin_clerk="adm171i")
    )
    with TestClient(app) as client:
        resp = client.get(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )
    entries = resp.json()["entries"]
    d1 = next(
        e for e in entries
        if e["user_id"] == str(seed["d1_user_id"])
    )
    assert d1["display_name"] == "Driver One"
    assert d1["skill_level"] == "intermediate"
    # Latest snapshot wins (82.5 > 70.0)
    assert d1["overall_score"] == 82.5
    assert d1["last_progress_at"] is not None
    # 2 open weaknesses seeded (1 open, 1 resolved); only open
    # counted
    assert d1["open_weaknesses"] == 1
    # 2 prescriptions (accepted, completed); only accepted counted
    assert d1["open_prescriptions"] == 1


def test_get_dashboard_driver_without_profile_returns_empty_entry(
    configured_app: dict[str, Any],
) -> None:
    """A driver-role member with no DriverProfile yet still
    appears in the dashboard with nullable identity and 0 counts.
    Pin: coaches see the full org roster, including unbootstrapped
    drivers — the roster doesn't omit just because the user
    hasn't completed profile setup."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_team(factory, admin_clerk="adm171j")
    )
    with TestClient(app) as client:
        resp = client.get(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )
    entries = resp.json()["entries"]
    d2 = next(
        e for e in entries
        if e["user_id"] == str(seed["d2_user_id"])
    )
    assert d2["display_name"] is None
    assert d2["skill_level"] is None
    assert d2["overall_score"] is None
    assert d2["last_progress_at"] is None
    assert d2["open_weaknesses"] == 0
    assert d2["open_prescriptions"] == 0
