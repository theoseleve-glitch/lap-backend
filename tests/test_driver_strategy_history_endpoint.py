"""T106 (sprint2-backend) — GET /api/v1/driver/strategy/history.

Driver-scoped list of the caller's recent sessions that have a
StrategyPlan attached (T096), each entry carrying the plan
summary and the persisted T102 execution_score. Powers the
webapp's "your strategy history" view so drivers can see how
their plan-vs-actual execution is trending session over session.

Acceptance:
  - 401 unauthenticated
  - 200 empty list for a driver with no strategy plans (distinct
    from 404 — the driver exists, they just haven't recorded a
    plan yet)
  - 200 ordered by session.created_at DESC (most-recent first —
    the canonical "recent" interpretation; webapp scrolls down
    for older entries)
  - driver isolation: only the caller's own sessions appear,
    other drivers' plans MUST be invisible (mutation pin: a
    missing driver_id filter would leak strategy across drivers,
    breaking ADR-005 RLS)
  - limit query param caps response size; default 20, max 100
  - execution_score=null entries are still listed (a plan
    without a finalized score is still part of history)
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


async def _seed_driver(
    factory: Any, *, clerk_id: str
) -> uuid6.UUID:
    """Seed a User + DriverProfile, return the driver_id."""
    async with factory() as db:
        user = User(
            id=uuid6.uuid7(), clerk_user_id=clerk_id,
            email=f"{clerk_id}@t",
        )
        driver = DriverProfile(
            id=uuid6.uuid7(), user_id=user.id, display_name=clerk_id,
        )
        db.add_all([user, driver])
        await db.commit()
        return driver.id


async def _seed_session_with_plan(
    factory: Any,
    *,
    driver_id: uuid6.UUID,
    session_created_at: datetime,
    planned_stints: int = 2,
    planned_fuel_liters: float = 80.0,
    planned_pit_laps: list[int] | None = None,
    execution_score: int | None = None,
) -> uuid6.UUID:
    """Create one session + one StrategyPlan; return session_id."""
    async with factory() as db:
        session = SessionRow(
            id=uuid6.uuid7(),
            driver_id=driver_id,
            manifest={},
            created_at=session_created_at,
            status="processed",
        )
        plan = StrategyPlan(
            id=uuid6.uuid7(),
            session_id=session.id,
            planned_stints=planned_stints,
            planned_fuel_liters=planned_fuel_liters,
            planned_pit_laps=planned_pit_laps or [30],
            execution_score=execution_score,
        )
        db.add_all([session, plan])
        await db.commit()
        return session.id


async def _seed_session_without_plan(
    factory: Any,
    *,
    driver_id: uuid6.UUID,
    session_created_at: datetime,
) -> uuid6.UUID:
    async with factory() as db:
        session = SessionRow(
            id=uuid6.uuid7(),
            driver_id=driver_id,
            manifest={},
            created_at=session_created_at,
            status="processed",
        )
        db.add(session)
        await db.commit()
        return session.id


def _get(
    client: TestClient, ca: dict[str, Any],
    *, clerk_id: str = "caller106", limit: int | None = None,
) -> Any:
    params = {"limit": limit} if limit is not None else {}
    return client.get(
        "/api/v1/driver/strategy/history",
        headers={
            "Authorization": f"Bearer {_bearer(ca['keys'], clerk_id)}"
        },
        params=params,
    )


# -- auth --------------------------------------------------------


def test_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    """No bearer → 401. Pin: history reveals strategy choices,
    owner-private."""
    with TestClient(app) as client:
        resp = client.get("/api/v1/driver/strategy/history")
    assert resp.status_code == 401


# -- empty list --------------------------------------------------


def test_driver_with_no_plans_returns_empty_list(
    configured_app: dict[str, Any],
) -> None:
    """Authed driver with zero StrategyPlans → 200 + empty list.
    Pin: must NOT be 404 — the driver exists, they just haven't
    recorded a plan yet. Distinct UI states."""
    factory = configured_app["factory"]
    asyncio.run(_seed_driver(factory, clerk_id="caller106"))
    with TestClient(app) as client:
        resp = _get(client, configured_app)
    assert resp.status_code == 200
    assert resp.json() == {"entries": []}


# -- ordering ----------------------------------------------------


def test_entries_ordered_by_session_created_at_desc(
    configured_app: dict[str, Any],
) -> None:
    """Three sessions seeded in NON-chronological order; the
    response must list them DESC by session.created_at (most
    recent first). Pin: ASC ordering would put the oldest plan
    at the top — the wrong "recent" interpretation."""
    factory = configured_app["factory"]
    driver_id = asyncio.run(_seed_driver(factory, clerk_id="caller106"))
    base = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)

    # Seed in middle-first order to prove ordering is by
    # created_at, not insertion order.
    middle_id = asyncio.run(_seed_session_with_plan(
        factory, driver_id=driver_id,
        session_created_at=base + timedelta(hours=1),
        planned_stints=2, execution_score=80,
    ))
    oldest_id = asyncio.run(_seed_session_with_plan(
        factory, driver_id=driver_id,
        session_created_at=base,
        planned_stints=1, execution_score=65,
    ))
    newest_id = asyncio.run(_seed_session_with_plan(
        factory, driver_id=driver_id,
        session_created_at=base + timedelta(hours=2),
        planned_stints=3, execution_score=90,
    ))

    with TestClient(app) as client:
        resp = _get(client, configured_app)
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert [e["session_id"] for e in entries] == [
        str(newest_id), str(middle_id), str(oldest_id),
    ]


# -- driver isolation -------------------------------------------


def test_other_drivers_plans_do_not_appear(
    configured_app: dict[str, Any],
) -> None:
    """Two drivers each have a StrategyPlan; the caller sees only
    their own. Pin: a missing driver_id filter would leak
    strategy across drivers — RLS bypass per ADR-005. Caught by
    asserting the OTHER driver's session_id is absent from the
    caller's response."""
    factory = configured_app["factory"]
    caller_driver = asyncio.run(_seed_driver(factory, clerk_id="caller106"))
    other_driver = asyncio.run(_seed_driver(factory, clerk_id="other106"))
    base = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)

    caller_session = asyncio.run(_seed_session_with_plan(
        factory, driver_id=caller_driver,
        session_created_at=base,
    ))
    other_session = asyncio.run(_seed_session_with_plan(
        factory, driver_id=other_driver,
        session_created_at=base + timedelta(hours=1),
    ))

    with TestClient(app) as client:
        resp = _get(client, configured_app)
    assert resp.status_code == 200
    session_ids = {e["session_id"] for e in resp.json()["entries"]}
    assert str(caller_session) in session_ids
    assert str(other_session) not in session_ids


# -- entry shape -------------------------------------------------


def test_entry_carries_plan_fields_and_execution_score(
    configured_app: dict[str, Any],
) -> None:
    """Single seeded plan; the entry exposes session_id,
    session_created_at, planned_stints, planned_fuel_liters,
    planned_pit_laps, and execution_score. Pin: the exact field
    set is the load-bearing contract for the webapp."""
    factory = configured_app["factory"]
    driver_id = asyncio.run(_seed_driver(factory, clerk_id="caller106"))
    when = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    session_id = asyncio.run(_seed_session_with_plan(
        factory, driver_id=driver_id,
        session_created_at=when,
        planned_stints=4, planned_fuel_liters=110.0,
        planned_pit_laps=[20, 40, 60], execution_score=78,
    ))

    with TestClient(app) as client:
        resp = _get(client, configured_app)
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["session_id"] == str(session_id)
    assert entry["planned_stints"] == 4
    assert entry["planned_fuel_liters"] == 110.0
    assert entry["planned_pit_laps"] == [20, 40, 60]
    assert entry["execution_score"] == 78


def test_entry_with_null_execution_score_is_listed(
    configured_app: dict[str, Any],
) -> None:
    """A plan with execution_score=None (not yet finalized) still
    appears in the history. Pin: filtering "not null" would hide
    in-flight sessions — webapp wants the whole picture."""
    factory = configured_app["factory"]
    driver_id = asyncio.run(_seed_driver(factory, clerk_id="caller106"))
    asyncio.run(_seed_session_with_plan(
        factory, driver_id=driver_id,
        session_created_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
        execution_score=None,
    ))

    with TestClient(app) as client:
        resp = _get(client, configured_app)
    entries = resp.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["execution_score"] is None


# -- limit -------------------------------------------------------


def test_default_limit_caps_at_20(
    configured_app: dict[str, Any],
) -> None:
    """Seed 25 plans; default limit returns 20. Pin: an unbounded
    response would scale with driver tenure and risk slow loads
    in heavy-use scenarios."""
    factory = configured_app["factory"]
    driver_id = asyncio.run(_seed_driver(factory, clerk_id="caller106"))
    base = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    for i in range(25):
        asyncio.run(_seed_session_with_plan(
            factory, driver_id=driver_id,
            session_created_at=base + timedelta(minutes=i),
        ))

    with TestClient(app) as client:
        resp = _get(client, configured_app)
    assert len(resp.json()["entries"]) == 20


def test_explicit_limit_respected(
    configured_app: dict[str, Any],
) -> None:
    """Caller asks for limit=3, gets 3 most-recent entries."""
    factory = configured_app["factory"]
    driver_id = asyncio.run(_seed_driver(factory, clerk_id="caller106"))
    base = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    for i in range(5):
        asyncio.run(_seed_session_with_plan(
            factory, driver_id=driver_id,
            session_created_at=base + timedelta(minutes=i),
        ))

    with TestClient(app) as client:
        resp = _get(client, configured_app, limit=3)
    assert len(resp.json()["entries"]) == 3


# -- sessions without plans excluded ---------------------------


def test_sessions_without_plans_are_excluded(
    configured_app: dict[str, Any],
) -> None:
    """Driver has 2 sessions; only one has a StrategyPlan. The
    history lists only the planned session. Pin: an outer-join
    on Session would leak plan-less sessions with null fields,
    polluting the strategy view with sprint-only sessions."""
    factory = configured_app["factory"]
    driver_id = asyncio.run(_seed_driver(factory, clerk_id="caller106"))
    base = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    planned_session = asyncio.run(_seed_session_with_plan(
        factory, driver_id=driver_id, session_created_at=base,
    ))
    asyncio.run(_seed_session_without_plan(
        factory, driver_id=driver_id,
        session_created_at=base + timedelta(hours=1),
    ))

    with TestClient(app) as client:
        resp = _get(client, configured_app)
    entries = resp.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["session_id"] == str(planned_session)
