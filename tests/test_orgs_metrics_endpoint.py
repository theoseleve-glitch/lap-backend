"""T174 (sprint2-backend) — GET /api/v1/orgs/:id/metrics.

Org-level aggregate KPIs for admin/coach dashboard:
  - wpd: distinct drivers in this org with at least one
    session uploaded in the trailing 7 days
  - best_gain_dimension + best_gain_value: across the org's
    drivers, the SkillDimensionName whose score grew most on
    average over the trailing 30 days. Null when fewer than
    2 snapshots exist per driver (no delta).

Auth doctrine (parallels T169/T171):
  - admin and coach in the org → 200
  - drivers and non-members → 403, status parity with
    unknown-org

Pinned behaviors:
  - 401 unauthenticated
  - 403 caller not in org
  - 403 unknown org
  - 403 caller is driver-role
  - 200 admin caller
  - 200 coach caller
  - 200 wpd counts distinct drivers (each driver counts once
    even with multiple sessions in window)
  - 200 wpd ignores sessions older than 7 days
  - 200 best_gain identifies the dimension with the largest
    average delta across the org's drivers
  - 200 best_gain is None when each driver has fewer than 2
    snapshots (no delta computable)
  - 200 wpd=0 and gain=None for an empty org (no driver data)
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
    DriverProfile,
    DriverProgressSnapshot,
    Organization,
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
    return f"/api/v1/orgs/{org_id}/metrics"


def _empty_dim() -> dict[str, Any]:
    return {}


def _snapshot(
    driver_id: Any,
    *,
    captured_at: datetime,
    braking_score: float = 70.0,
    racecraft_score: float = 70.0,
) -> DriverProgressSnapshot:
    """Helper — only the dimensions a test pins are populated;
    the rest default to empty dicts (the score-gain logic
    falls back to None when a dim has no `score` key)."""
    return DriverProgressSnapshot(
        id=uuid6.uuid7(),
        driver_id=driver_id,
        captured_at=captured_at,
        window="trailing_30d",
        window_start=captured_at - timedelta(days=30),
        window_end=captured_at,
        braking={"score": braking_score},
        throttle_pickup=_empty_dim(),
        minimum_speed=_empty_dim(),
        line_consistency=_empty_dim(),
        tire_management=_empty_dim(),
        racecraft={"score": racecraft_score},
        fatigue_consistency=_empty_dim(),
        strategy_execution=_empty_dim(),
        setup_sensitivity=_empty_dim(),
        adaptation=_empty_dim(),
        overall_score=(braking_score + racecraft_score) / 2,
        overall_confidence=0.7,
        percentile_vs_cohort=50.0,
        generated_by="test",
    )


async def _seed_basic_org(
    factory: Any, *, admin_clerk: str
) -> dict[str, Any]:
    """Empty org — admin + coach + 1 driver-with-profile, no
    sessions / snapshots."""
    async with factory() as db:
        admin = User(
            id=uuid6.uuid7(),
            clerk_user_id=admin_clerk,
            email=f"{admin_clerk}@t",
        )
        coach = User(
            id=uuid6.uuid7(),
            clerk_user_id=f"coach174_{secrets.token_hex(3)}",
            email=f"coach174_{secrets.token_hex(3)}@t",
        )
        driver = User(
            id=uuid6.uuid7(),
            clerk_user_id=f"drv174_{secrets.token_hex(3)}",
            email=f"drv174_{secrets.token_hex(3)}@t",
        )
        db.add_all([admin, coach, driver])
        await db.flush()
        org = Organization(
            id=uuid6.uuid7(),
            name="T174 Org",
            plan="team",
            admin_user_id=admin.id,
            seats_limit=20,
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
            display_name="d",
            skill_level="intermediate",
            cohort_opt_in=False,
        )
        db.add(profile)
        await db.commit()
        return {
            "org_id": org.id,
            "admin_clerk": admin.clerk_user_id,
            "coach_clerk": coach.clerk_user_id,
            "driver_clerk": driver.clerk_user_id,
            "driver_user_id": driver.id,
            "driver_profile_id": profile.id,
        }


# -- auth + RLS --------------------------------------------------


def test_get_metrics_unauthenticated_returns_401(
    configured_app: dict[str, Any],
) -> None:
    seed = asyncio.run(
        _seed_basic_org(
            configured_app["factory"], admin_clerk="adm174a"
        )
    )
    with TestClient(app) as client:
        resp = client.get(_url(seed["org_id"]))
    assert resp.status_code == 401


def test_get_metrics_caller_not_in_org_returns_403(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_basic_org(factory, admin_clerk="adm174b")
    )

    async def _seed_outsider() -> None:
        async with factory() as db:
            db.add(
                User(
                    id=uuid6.uuid7(),
                    clerk_user_id="outsider174",
                    email="outsider174@t",
                )
            )
            await db.commit()
    asyncio.run(_seed_outsider())

    with TestClient(app) as client:
        resp = client.get(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, "outsider174"),
        )
    assert resp.status_code == 403


def test_get_metrics_unknown_org_returns_403(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_basic_org(factory, admin_clerk="adm174c")
    )
    fake_org = uuid6.uuid7()
    with TestClient(app) as client:
        resp = client.get(
            _url(fake_org),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )
    assert resp.status_code == 403


def test_get_metrics_caller_is_driver_returns_403(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_basic_org(factory, admin_clerk="adm174d")
    )
    with TestClient(app) as client:
        resp = client.get(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["driver_clerk"]),
        )
    assert resp.status_code == 403


# -- happy paths --------------------------------------------------


def test_get_metrics_admin_returns_200(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_basic_org(factory, admin_clerk="adm174e")
    )
    with TestClient(app) as client:
        resp = client.get(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )
    assert resp.status_code == 200, resp.text


def test_get_metrics_coach_returns_200(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_basic_org(factory, admin_clerk="adm174f")
    )
    with TestClient(app) as client:
        resp = client.get(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["coach_clerk"]),
        )
    assert resp.status_code == 200, resp.text


def test_get_metrics_empty_org_returns_zero_and_null(
    configured_app: dict[str, Any],
) -> None:
    """Org with members but no sessions/snapshots: wpd=0, both
    gain fields None. Pin: endpoint never 5xx's on data
    thinness."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_basic_org(factory, admin_clerk="adm174g")
    )
    with TestClient(app) as client:
        resp = client.get(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )
    body = resp.json()
    assert body == {
        "wpd": 0,
        "best_gain_dimension": None,
        "best_gain_value": None,
    }


def test_get_metrics_wpd_counts_distinct_drivers_with_recent_sessions(
    configured_app: dict[str, Any],
) -> None:
    """wpd = count of distinct drivers in the org with ≥1
    session in the last 7 days. Multiple sessions by the same
    driver still count as 1 driver. Pin: distinct-by-driver
    semantics matches the global /admin/kpis WPD."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_basic_org(factory, admin_clerk="adm174h")
    )

    async def _add_sessions() -> None:
        async with factory() as db:
            now = datetime.now(UTC)
            # 3 sessions from same driver — should count as 1
            for _ in range(3):
                db.add(
                    SessionRow(
                        id=uuid6.uuid7(),
                        driver_id=seed["driver_profile_id"],
                        manifest={},
                        created_at=now - timedelta(days=2),
                    )
                )
            await db.commit()
    asyncio.run(_add_sessions())

    with TestClient(app) as client:
        resp = client.get(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )
    assert resp.json()["wpd"] == 1


def test_get_metrics_wpd_excludes_old_sessions(
    configured_app: dict[str, Any],
) -> None:
    """Sessions older than the 7-day window don't count toward
    wpd. Pin: window-correctness — without it the metric loses
    its 'this week' meaning."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_basic_org(factory, admin_clerk="adm174i")
    )

    async def _add_old_session() -> None:
        async with factory() as db:
            now = datetime.now(UTC)
            db.add(
                SessionRow(
                    id=uuid6.uuid7(),
                    driver_id=seed["driver_profile_id"],
                    manifest={},
                    created_at=now - timedelta(days=14),
                )
            )
            await db.commit()
    asyncio.run(_add_old_session())

    with TestClient(app) as client:
        resp = client.get(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )
    assert resp.json()["wpd"] == 0


def test_get_metrics_best_gain_picks_largest_avg_delta(
    configured_app: dict[str, Any],
) -> None:
    """Driver with 2 snapshots: braking 70 → 80 (+10),
    racecraft 60 → 65 (+5). Best gain = braking @ +10.
    Pin: deltas are computed per-dim per-driver, then averaged
    across drivers per dim, then max picked."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_basic_org(factory, admin_clerk="adm174j")
    )

    async def _add_snaps() -> None:
        async with factory() as db:
            now = datetime.now(UTC)
            db.add(
                _snapshot(
                    seed["driver_profile_id"],
                    captured_at=now - timedelta(days=20),
                    braking_score=70.0,
                    racecraft_score=60.0,
                )
            )
            db.add(
                _snapshot(
                    seed["driver_profile_id"],
                    captured_at=now - timedelta(days=2),
                    braking_score=80.0,
                    racecraft_score=65.0,
                )
            )
            await db.commit()
    asyncio.run(_add_snaps())

    with TestClient(app) as client:
        resp = client.get(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )
    body = resp.json()
    assert body["best_gain_dimension"] == "braking"
    assert body["best_gain_value"] == pytest.approx(10.0)


def test_get_metrics_best_gain_null_with_single_snapshot(
    configured_app: dict[str, Any],
) -> None:
    """One snapshot per driver → no delta computable. Pin:
    surfaces the no-data-yet case as null rather than 0
    (which would be ambiguous: 0 = 'no gain' vs 'no data')."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_basic_org(factory, admin_clerk="adm174k")
    )

    async def _add_snap() -> None:
        async with factory() as db:
            db.add(
                _snapshot(
                    seed["driver_profile_id"],
                    captured_at=datetime.now(UTC) - timedelta(days=2),
                    braking_score=70.0,
                )
            )
            await db.commit()
    asyncio.run(_add_snap())

    with TestClient(app) as client:
        resp = client.get(
            _url(seed["org_id"]),
            headers=_hdrs(configured_app, seed["admin_clerk"]),
        )
    body = resp.json()
    assert body["best_gain_dimension"] is None
    assert body["best_gain_value"] is None
