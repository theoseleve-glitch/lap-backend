"""T105 (sprint2-backend) — GET /api/v1/sessions/{id}/strategy/visualization.

Aggregated read for the webapp's plan-vs-actual chart. Bundles
T096's StrategyPlan, the actual pit/fuel/stint outcomes derived
from T097's StrategyEvent timeline, and T102's persisted
execution_score in a single round trip — the webapp draws the
chart without making three sequential calls or rebuilding the
event-to-actuals math client-side.

Why a separate endpoint vs reusing T100 timeline:
  - T100 returns raw (plan, events) — useful for any consumer
    that wants the full event stream
  - T105 returns the visualization-shaped aggregates: actual
    pit-laps as a sorted tuple, fuel total summed across pit
    events, stint lengths derived from stint_end events. The
    webapp consumes shapes ready for chart axes.

Acceptance:
  - 401 unauthenticated
  - 404 when session doesn't exist OR caller doesn't own it
    (status parity prevents enumeration)
  - 200 empty shape (plan=null, all actuals empty/0,
    execution_score=null) for an owned session with no strategy
    data — distinct from 404
  - 200 full shape with the plan, derived actuals in lap order,
    and the persisted execution_score
  - actual_fuel_liters_total = sum of `fuel_added_liters` across
    pit events; missing key in details treated as 0 (tire-only
    stops legitimately omit it; mirrors T101's behavior)
  - actual_stint_lengths derived from stint_end laps: stint 1 =
    first end_lap, stint N = end_lap[N] - end_lap[N-1]
  - actual_pit_laps tuple sorted ASC (chart x-axis)
"""

from __future__ import annotations

import asyncio
import base64
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
    StrategyEvent,
    StrategyPlan,
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
            "sub": clerk_user_id, "email": f"{clerk_user_id}@t",
            "exp": int(time.time()) + 3600,
        },
        keys["kid"],
    )


async def _seed_owned_session(
    factory: Any, *, clerk_id: str
) -> uuid6.UUID:
    async with factory() as db:
        user = User(
            id=uuid6.uuid7(), clerk_user_id=clerk_id,
            email=f"{clerk_id}@t",
        )
        driver = DriverProfile(
            id=uuid6.uuid7(), user_id=user.id, display_name=clerk_id,
        )
        session = SessionRow(
            id=uuid6.uuid7(),
            driver_id=driver.id,
            manifest={},
            created_at=datetime(2026, 4, 27, tzinfo=UTC),
            status="processed",
        )
        db.add_all([user, driver, session])
        await db.commit()
        return session.id


async def _seed_other_drivers_session(factory: Any) -> uuid6.UUID:
    async with factory() as db:
        user = User(
            id=uuid6.uuid7(), clerk_user_id="other105",
            email="other105@t",
        )
        driver = DriverProfile(
            id=uuid6.uuid7(), user_id=user.id, display_name="other105",
        )
        session = SessionRow(
            id=uuid6.uuid7(),
            driver_id=driver.id,
            manifest={},
            created_at=datetime(2026, 4, 27, tzinfo=UTC),
            status="processed",
        )
        db.add_all([user, driver, session])
        await db.commit()
        return session.id


def _get(
    client: TestClient, ca: dict[str, Any],
    *, session_id: uuid6.UUID, clerk_id: str = "caller105",
) -> Any:
    return client.get(
        f"/api/v1/sessions/{session_id}/strategy/visualization",
        headers={
            "Authorization": f"Bearer {_bearer(ca['keys'], clerk_id)}"
        },
    )


# -- auth --------------------------------------------------------


def test_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    """No bearer → 401. Pin: visualization data leaks the
    driver's pit/fuel/stint choices, owner-private."""
    with TestClient(app) as client:
        resp = client.get(
            f"/api/v1/sessions/{uuid6.uuid7()}/strategy/visualization",
        )
    assert resp.status_code == 401


# -- ownership scoping ------------------------------------------


def test_get_other_drivers_session_returns_404(
    configured_app: dict[str, Any],
) -> None:
    """Caller authed but session belongs to another driver →
    same 404 as 'not found'. Pin: status parity prevents
    enumeration via visualization reads."""
    factory = configured_app["factory"]
    asyncio.run(_seed_owned_session(factory, clerk_id="caller105"))
    other_session_id = asyncio.run(_seed_other_drivers_session(factory))
    with TestClient(app) as client:
        resp = _get(client, configured_app, session_id=other_session_id)
    assert resp.status_code == 404


def test_get_unknown_session_returns_404(
    configured_app: dict[str, Any],
) -> None:
    asyncio.run(_seed_owned_session(configured_app["factory"], clerk_id="caller105"))
    with TestClient(app) as client:
        resp = _get(client, configured_app, session_id=uuid6.uuid7())
    assert resp.status_code == 404


# -- empty visualization ----------------------------------------


def test_session_with_no_plan_or_events_returns_empty_shape(
    configured_app: dict[str, Any],
) -> None:
    """Driver owns the session but never set a plan or logged
    events → 200 with plan=null, all actuals empty/0,
    execution_score=null. Pin: must NOT be a 404 — the session
    exists, it just has no strategy data; the UI shows "no
    strategy recorded" vs "session not found" as different
    states."""
    factory = configured_app["factory"]
    session_id = asyncio.run(
        _seed_owned_session(factory, clerk_id="caller105")
    )
    with TestClient(app) as client:
        resp = _get(client, configured_app, session_id=session_id)
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan"] is None
    assert body["actual_pit_laps"] == []
    assert body["actual_fuel_liters_total"] == 0.0
    assert body["actual_stint_lengths"] == []
    assert body["execution_score"] is None


# -- full visualization -----------------------------------------


def test_returns_plan_actuals_and_score(
    configured_app: dict[str, Any],
) -> None:
    """Plan + 3 events seeded out-of-lap-order; response carries
    plan, actual_pit_laps sorted ASC, summed fuel, derived stint
    lengths, and the persisted execution_score.

    Pins:
      - actual_pit_laps is sorted ASC even when seeded out-of-order
      - actual_fuel_liters_total sums fuel_added_liters across
        pit events; missing key contributes 0
      - actual_stint_lengths is derived from stint_end laps:
        first stint = first end_lap, subsequent = end_lap[i] -
        end_lap[i-1]
      - execution_score round-trips from the StrategyPlan row"""
    factory = configured_app["factory"]
    session_id = asyncio.run(
        _seed_owned_session(factory, clerk_id="caller105")
    )
    base = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)

    async def seed() -> None:
        async with factory() as db:
            db.add(
                StrategyPlan(
                    id=uuid6.uuid7(),
                    session_id=session_id,
                    planned_stints=2,
                    planned_fuel_liters=80.0,
                    planned_pit_laps=[30],
                    execution_score=72,
                )
            )
            db.add_all([
                # Pit events seeded out of lap order; expect ASC.
                StrategyEvent(
                    id=uuid6.uuid7(),
                    session_id=session_id,
                    kind="pit",
                    at_lap=58,
                    details={"fuel_added_liters": 35.5},
                    created_at=base + timedelta(seconds=10),
                ),
                StrategyEvent(
                    id=uuid6.uuid7(),
                    session_id=session_id,
                    kind="pit",
                    at_lap=28,
                    details={"fuel_added_liters": 40.0},
                    created_at=base,
                ),
                # Tire-only stop with no fuel_added_liters key.
                StrategyEvent(
                    id=uuid6.uuid7(),
                    session_id=session_id,
                    kind="pit",
                    at_lap=44,
                    details={"tires": "softs"},
                    created_at=base + timedelta(seconds=5),
                ),
                # Stint ends — chronological for stint-length math.
                StrategyEvent(
                    id=uuid6.uuid7(),
                    session_id=session_id,
                    kind="stint_end",
                    at_lap=28,
                    details={},
                    created_at=base + timedelta(seconds=11),
                ),
                StrategyEvent(
                    id=uuid6.uuid7(),
                    session_id=session_id,
                    kind="stint_end",
                    at_lap=58,
                    details={},
                    created_at=base + timedelta(seconds=12),
                ),
            ])
            await db.commit()

    asyncio.run(seed())

    with TestClient(app) as client:
        resp = _get(client, configured_app, session_id=session_id)
    assert resp.status_code == 200
    body = resp.json()

    assert body["plan"] is not None
    assert body["plan"]["planned_stints"] == 2
    assert body["plan"]["planned_pit_laps"] == [30]

    # Sorted ASC even though seeded as 58, 28, 44.
    assert body["actual_pit_laps"] == [28, 44, 58]

    # 40.0 + 0 (missing key) + 35.5 = 75.5
    assert body["actual_fuel_liters_total"] == 75.5

    # Stint lengths: first = 28, second = 58 - 28 = 30.
    assert body["actual_stint_lengths"] == [28, 30]

    assert body["execution_score"] == 72


def test_returns_zero_fuel_when_all_pit_events_omit_fuel(
    configured_app: dict[str, Any],
) -> None:
    """Defense in depth: every pit event omits `fuel_added_liters`
    (tire-only stops). Pin: the sum must be exactly 0.0, not raise
    KeyError. Mirrors T101's defensive `details.get(..., 0)`
    handling — drift here would silently break sessions where the
    capture pipeline doesn't emit the fuel key."""
    factory = configured_app["factory"]
    session_id = asyncio.run(
        _seed_owned_session(factory, clerk_id="caller105")
    )
    base = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)

    async def seed() -> None:
        async with factory() as db:
            db.add_all([
                StrategyEvent(
                    id=uuid6.uuid7(),
                    session_id=session_id,
                    kind="pit",
                    at_lap=20,
                    details={"tires": "mediums"},
                    created_at=base,
                ),
                StrategyEvent(
                    id=uuid6.uuid7(),
                    session_id=session_id,
                    kind="pit",
                    at_lap=40,
                    details={},
                    created_at=base + timedelta(seconds=1),
                ),
            ])
            await db.commit()

    asyncio.run(seed())

    with TestClient(app) as client:
        resp = _get(client, configured_app, session_id=session_id)
    assert resp.status_code == 200
    assert resp.json()["actual_fuel_liters_total"] == 0.0
