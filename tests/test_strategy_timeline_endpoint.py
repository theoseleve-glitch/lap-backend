"""T100 (sprint2-backend) — GET /api/v1/sessions/{id}/strategy/timeline.

Combined plan-and-actual timeline for a session. Returns:
  - the canonical StrategyPlan (T096) if set, else null
  - the StrategyEvent (T097) timeline ordered by lap

The UI renders the two side-by-side: planned pit-laps next to
actual pit events, planned stints next to actual stint_end
events, etc. Plan-vs-actual divergence scoring (T101+) consumes
this same shape.

Acceptance:
  - 401 unauthenticated
  - 404 when session doesn't exist OR caller doesn't own it
  - 200 with plan=null + events=[] for a session that has
    neither (cleanly distinct from 404)
  - 200 with the plan AND every event, events ordered by
    (at_lap ASC, created_at ASC) so plan-vs-actual diff
    iterates in lap order
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
            id=uuid6.uuid7(), clerk_user_id="other100",
            email="other100@t",
        )
        driver = DriverProfile(
            id=uuid6.uuid7(), user_id=user.id, display_name="other100",
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
    *, session_id: uuid6.UUID, clerk_id: str = "caller",
) -> Any:
    return client.get(
        f"/api/v1/sessions/{session_id}/strategy/timeline",
        headers={
            "Authorization": f"Bearer {_bearer(ca['keys'], clerk_id)}"
        },
    )


# -- auth --------------------------------------------------------


def test_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    """No bearer → 401. Pin: timeline reveals strategy choices
    that are owner-private."""
    with TestClient(app) as client:
        resp = client.get(
            f"/api/v1/sessions/{uuid6.uuid7()}/strategy/timeline",
        )
    assert resp.status_code == 401


# -- ownership scoping ------------------------------------------


def test_get_other_drivers_session_returns_404(
    configured_app: dict[str, Any],
) -> None:
    """Caller authed but session belongs to another driver →
    same 404 as 'not found'. Pin: status parity prevents
    enumeration via timeline reads."""
    factory = configured_app["factory"]
    asyncio.run(_seed_owned_session(factory, clerk_id="caller"))
    other_session_id = asyncio.run(_seed_other_drivers_session(factory))
    with TestClient(app) as client:
        resp = _get(client, configured_app, session_id=other_session_id)
    assert resp.status_code == 404


def test_get_unknown_session_returns_404(
    configured_app: dict[str, Any],
) -> None:
    asyncio.run(_seed_owned_session(configured_app["factory"], clerk_id="caller"))
    with TestClient(app) as client:
        resp = _get(client, configured_app, session_id=uuid6.uuid7())
    assert resp.status_code == 404


# -- empty timeline ---------------------------------------------


def test_session_with_no_plan_or_events_returns_empty_shape(
    configured_app: dict[str, Any],
) -> None:
    """Driver owns the session but never set a plan or logged
    events → 200 with plan=null and empty events. Pin: must
    NOT be a 404 — the session exists, it just has no strategy
    data; the UI shows "no strategy recorded" vs "session not
    found" as different states."""
    factory = configured_app["factory"]
    session_id = asyncio.run(_seed_owned_session(factory, clerk_id="caller"))
    with TestClient(app) as client:
        resp = _get(client, configured_app, session_id=session_id)
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan"] is None
    assert body["events"] == []


# -- combined timeline ------------------------------------------


def test_returns_plan_and_events_in_lap_order(
    configured_app: dict[str, Any],
) -> None:
    """Plan + 3 events seeded out-of-lap-order; response carries
    the plan AND every event sorted by (at_lap ASC, created_at
    ASC). Pin: ordering is the load-bearing contract — the
    plan-vs-actual diff (T101) iterates in lap order; insertion
    order or DESC would mis-compare."""
    factory = configured_app["factory"]
    session_id = asyncio.run(_seed_owned_session(factory, clerk_id="caller"))
    base = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)

    async def seed_strategy_data() -> None:
        async with factory() as db:
            db.add(
                StrategyPlan(
                    id=uuid6.uuid7(),
                    session_id=session_id,
                    planned_stints=2,
                    planned_fuel_liters=80.0,
                    planned_pit_laps=[30],
                )
            )
            # Inserted out of lap order to prove sort-by-at_lap.
            db.add_all([
                StrategyEvent(
                    id=uuid6.uuid7(),
                    session_id=session_id,
                    kind="pit",
                    at_lap=28,
                    details={"stint_index": 1},
                    created_at=base + timedelta(seconds=10),
                ),
                StrategyEvent(
                    id=uuid6.uuid7(),
                    session_id=session_id,
                    kind="plan",
                    at_lap=0,
                    details={"planned_stints": 2},
                    created_at=base,
                ),
                StrategyEvent(
                    id=uuid6.uuid7(),
                    session_id=session_id,
                    kind="stint_end",
                    at_lap=28,
                    details={"stint_index": 1},
                    created_at=base + timedelta(seconds=11),
                ),
            ])
            await db.commit()

    asyncio.run(seed_strategy_data())

    with TestClient(app) as client:
        resp = _get(client, configured_app, session_id=session_id)
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan"] is not None
    assert body["plan"]["planned_stints"] == 2
    assert body["plan"]["planned_pit_laps"] == [30]
    # Lap-order: lap 0 plan, then lap 28 pit then stint_end
    # (created_at tie-breaks within the same lap).
    assert [e["kind"] for e in body["events"]] == [
        "plan", "pit", "stint_end",
    ]
    assert [e["at_lap"] for e in body["events"]] == [0, 28, 28]
