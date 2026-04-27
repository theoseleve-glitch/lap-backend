"""T109 (sprint2-backend) — POST /api/v1/sessions/{id}/strategy/simulate.

"What if I pitted 2 laps later?" — given an actual session with
its event log already recorded, this endpoint scores a
hypothetical alternate plan against the same actual events. The
caller submits a StrategyPlanCreate-shaped body plus a
total_race_laps and gets back the divergence deltas (T101) and
execution score (T102) the hypothetical would have produced.

Read-only: doesn't write to StrategyPlan or StrategyEvent.
Distinct from T098's POST /strategy/plan which persists; this
endpoint is a pure what-if calculator.

Uses T108's caution-aware divergence so the simulated score
reflects the cautions that actually occurred in the race —
matches the real-world counterfactual ("if I had pitted
differently with the SAME race conditions").

Acceptance:
  - 401 unauthenticated
  - 404 when session doesn't exist OR caller doesn't own it
    (status parity prevents enumeration)
  - 200 returns pit_lap_delta_total, fuel_delta_liters,
    stint_duration_delta_laps, execution_score
  - 422 on malformed plan (planned_stints<1, fuel<=0,
    total_race_laps<=0)
  - simulating the SAME plan as actual produces low deltas /
    high score (sanity)
  - simulating a worse-than-actual plan produces measurably
    different deltas (counterfactual works)
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
            id=uuid6.uuid7(), clerk_user_id="other109",
            email="other109@t",
        )
        driver = DriverProfile(
            id=uuid6.uuid7(), user_id=user.id, display_name="other109",
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


async def _seed_actual_events(
    factory: Any, *, session_id: uuid6.UUID,
) -> None:
    """Seed events: pit at lap 30 (40L fuel), stint_end at 30 + 60."""
    base = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    async with factory() as db:
        db.add_all([
            StrategyEvent(
                id=uuid6.uuid7(),
                session_id=session_id,
                kind="pit",
                at_lap=30,
                details={"fuel_added_liters": 40.0},
                created_at=base,
            ),
            StrategyEvent(
                id=uuid6.uuid7(),
                session_id=session_id,
                kind="stint_end",
                at_lap=30,
                details={},
                created_at=base + timedelta(seconds=1),
            ),
            StrategyEvent(
                id=uuid6.uuid7(),
                session_id=session_id,
                kind="stint_end",
                at_lap=60,
                details={},
                created_at=base + timedelta(seconds=2),
            ),
        ])
        await db.commit()


def _post(
    client: TestClient,
    ca: dict[str, Any],
    *,
    session_id: uuid6.UUID,
    body: dict[str, Any],
    clerk_id: str = "caller109",
) -> Any:
    return client.post(
        f"/api/v1/sessions/{session_id}/strategy/simulate",
        headers={
            "Authorization": f"Bearer {_bearer(ca['keys'], clerk_id)}",
        },
        json=body,
    )


def _matching_plan_body() -> dict[str, Any]:
    """Hypothetical plan that matches the seeded actuals exactly:
    pit at lap 30, 40L fuel, 2 stints over 60 race laps."""
    return {
        "planned_stints": 2,
        "planned_fuel_liters": 40.0,
        "planned_pit_laps": [30],
        "total_race_laps": 60,
    }


# -- auth --------------------------------------------------------


def test_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    """No bearer → 401. Pin: simulation reveals strategy
    inferences over actual session data, owner-private."""
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/sessions/{uuid6.uuid7()}/strategy/simulate",
            json=_matching_plan_body(),
        )
    assert resp.status_code == 401


# -- ownership scoping ------------------------------------------


def test_simulate_other_drivers_session_returns_404(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    asyncio.run(_seed_owned_session(factory, clerk_id="caller109"))
    other_session_id = asyncio.run(_seed_other_drivers_session(factory))
    with TestClient(app) as client:
        resp = _post(
            client, configured_app,
            session_id=other_session_id, body=_matching_plan_body(),
        )
    assert resp.status_code == 404


def test_simulate_unknown_session_returns_404(
    configured_app: dict[str, Any],
) -> None:
    asyncio.run(_seed_owned_session(
        configured_app["factory"], clerk_id="caller109",
    ))
    with TestClient(app) as client:
        resp = _post(
            client, configured_app,
            session_id=uuid6.uuid7(), body=_matching_plan_body(),
        )
    assert resp.status_code == 404


# -- response shape ---------------------------------------------


def test_returns_divergence_deltas_and_score(
    configured_app: dict[str, Any],
) -> None:
    """Hypothetical plan that matches actuals exactly → all three
    deltas are 0 and execution_score is 100. Pin: the response
    shape MUST carry pit_lap_delta_total, fuel_delta_liters,
    stint_duration_delta_laps, execution_score — that's the
    load-bearing contract for the webapp's what-if UI."""
    factory = configured_app["factory"]
    session_id = asyncio.run(
        _seed_owned_session(factory, clerk_id="caller109"),
    )
    asyncio.run(_seed_actual_events(factory, session_id=session_id))
    with TestClient(app) as client:
        resp = _post(
            client, configured_app,
            session_id=session_id, body=_matching_plan_body(),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["pit_lap_delta_total"] == 0
    assert body["fuel_delta_liters"] == 0
    assert body["stint_duration_delta_laps"] == 0
    assert body["execution_score"] == 100


def test_simulating_pit_two_laps_later_increases_pit_delta(
    configured_app: dict[str, Any],
) -> None:
    """The canonical T109 use case: actual pit at lap 30; simulate
    the hypothetical "what if I had planned to pit at lap 32?".
    The pit_lap_delta_total must be exactly 2 (|32 - 30|) — the
    actual happened at 30, the hypothetical plan said 32, so the
    miss is 2 laps. Pin: this is the exact spec wording — "what
    if I pitted 2 laps later"."""
    factory = configured_app["factory"]
    session_id = asyncio.run(
        _seed_owned_session(factory, clerk_id="caller109"),
    )
    asyncio.run(_seed_actual_events(factory, session_id=session_id))
    with TestClient(app) as client:
        resp = _post(
            client, configured_app,
            session_id=session_id,
            body={
                "planned_stints": 2,
                "planned_fuel_liters": 40.0,
                "planned_pit_laps": [32],
                "total_race_laps": 60,
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["pit_lap_delta_total"] == 2


def test_does_not_persist_a_strategy_plan(
    configured_app: dict[str, Any],
) -> None:
    """Read-only: simulating must NOT create a StrategyPlan row.
    Pin: T109 is a pure what-if calculator; the canonical
    StrategyPlan goes through T098's POST endpoint and is
    UNIQUE per session. A simulator side-effecting writes would
    block subsequent legitimate POSTs with a 409."""
    factory = configured_app["factory"]
    session_id = asyncio.run(
        _seed_owned_session(factory, clerk_id="caller109"),
    )
    asyncio.run(_seed_actual_events(factory, session_id=session_id))
    with TestClient(app) as client:
        resp = _post(
            client, configured_app,
            session_id=session_id, body=_matching_plan_body(),
        )
    assert resp.status_code == 200

    async def _no_plan() -> bool:
        from sqlalchemy import select

        from infra.orm import StrategyPlan
        async with factory() as db:
            row = (
                await db.execute(
                    select(StrategyPlan).where(
                        StrategyPlan.session_id == session_id
                    )
                )
            ).scalar_one_or_none()
            return row is None
    assert asyncio.run(_no_plan())


# -- input validation -------------------------------------------


def test_rejects_zero_total_race_laps(
    configured_app: dict[str, Any],
) -> None:
    """total_race_laps must be > 0 — without it the divergence
    math has no race scaffold; rejecting at the boundary catches
    typos before the runner short-circuits to all-zeros."""
    factory = configured_app["factory"]
    session_id = asyncio.run(
        _seed_owned_session(factory, clerk_id="caller109"),
    )
    with TestClient(app) as client:
        resp = _post(
            client, configured_app,
            session_id=session_id,
            body={
                "planned_stints": 2,
                "planned_fuel_liters": 40.0,
                "planned_pit_laps": [30],
                "total_race_laps": 0,
            },
        )
    assert resp.status_code == 422


def test_rejects_zero_planned_stints(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    session_id = asyncio.run(
        _seed_owned_session(factory, clerk_id="caller109"),
    )
    with TestClient(app) as client:
        resp = _post(
            client, configured_app,
            session_id=session_id,
            body={
                "planned_stints": 0,
                "planned_fuel_liters": 40.0,
                "planned_pit_laps": [30],
                "total_race_laps": 60,
            },
        )
    assert resp.status_code == 422
