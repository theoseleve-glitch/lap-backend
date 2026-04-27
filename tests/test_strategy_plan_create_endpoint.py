"""T098 (sprint2-backend) — POST /api/v1/sessions/{id}/strategy/plan.

Sets the pre-race strategy plan for a session: stint count,
fuel budget, intended pit-lap numbers. Backed by T096's
StrategyPlan ORM (UNIQUE on session_id), so a second POST on
the same session returns 409 instead of silently overwriting.

Acceptance:
  - 401 unauthenticated
  - 404 when session doesn't exist OR caller doesn't own it
    (same status either way to avoid enumeration)
  - 422 on invalid body (planned_stints < 1, planned_fuel <= 0)
  - 201 with the persisted StrategyPlan on first call
  - 409 conflict on second call (audit trail preserved via
    StrategyEvent log per T097, not by mutating the row)
  - response carries server-stamped id + created_at
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
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


async def _seed_owned_session(
    factory: Any, *, clerk_id: str
) -> uuid6.UUID:
    """User + DriverProfile + Session that the user owns."""
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
    """A different driver + their own session — used to prove
    the caller cannot POST a plan onto someone else's session."""
    async with factory() as db:
        user = User(
            id=uuid6.uuid7(), clerk_user_id="other",
            email="other@t",
        )
        driver = DriverProfile(
            id=uuid6.uuid7(), user_id=user.id, display_name="other",
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


def _post(
    client: TestClient,
    ca: dict[str, Any],
    *,
    session_id: uuid6.UUID,
    body: dict[str, Any],
    clerk_id: str = "caller",
) -> Any:
    return client.post(
        f"/api/v1/sessions/{session_id}/strategy/plan",
        headers={
            "Authorization": f"Bearer {_bearer(ca['keys'], clerk_id)}"
        },
        json=body,
    )


_VALID_BODY: dict[str, Any] = {
    "planned_stints": 3,
    "planned_fuel_liters": 120.0,
    "planned_pit_laps": [25, 50],
}


# -- auth --------------------------------------------------------


def test_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    """No bearer → 401. Pin: a session's strategy plan is owner-
    private; an unauthenticated POST must never persist."""
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/sessions/{uuid6.uuid7()}/strategy/plan",
            json=_VALID_BODY,
        )
    assert resp.status_code == 401


# -- ownership scoping ------------------------------------------


def test_post_to_other_drivers_session_returns_404(
    configured_app: dict[str, Any],
) -> None:
    """Caller is authed but the session belongs to another
    driver. Pin: 404 (not 403) — same status as "session
    doesn't exist" so an attacker cannot enumerate which UUIDs
    correspond to other drivers' sessions."""
    factory = configured_app["factory"]
    asyncio.run(_seed_owned_session(factory, clerk_id="caller"))
    other_session_id = asyncio.run(_seed_other_drivers_session(factory))
    with TestClient(app) as client:
        resp = _post(
            client, configured_app,
            session_id=other_session_id, body=_VALID_BODY,
        )
    assert resp.status_code == 404


def test_post_to_unknown_session_returns_404(
    configured_app: dict[str, Any],
) -> None:
    """Session UUID doesn't exist at all → same 404 as "exists
    but not yours". Pin: status parity prevents the
    enumeration oracle described above."""
    asyncio.run(_seed_owned_session(configured_app["factory"], clerk_id="caller"))
    with TestClient(app) as client:
        resp = _post(
            client, configured_app,
            session_id=uuid6.uuid7(), body=_VALID_BODY,
        )
    assert resp.status_code == 404


# -- validation -------------------------------------------------


def test_invalid_body_planned_stints_zero_returns_422(
    configured_app: dict[str, Any],
) -> None:
    """planned_stints must be >= 1; a 0-stint plan is meaningless."""
    factory = configured_app["factory"]
    session_id = asyncio.run(_seed_owned_session(factory, clerk_id="caller"))
    bad = {**_VALID_BODY, "planned_stints": 0}
    with TestClient(app) as client:
        resp = _post(client, configured_app, session_id=session_id, body=bad)
    assert resp.status_code == 422


def test_invalid_body_planned_fuel_zero_returns_422(
    configured_app: dict[str, Any],
) -> None:
    """planned_fuel_liters must be > 0; a no-fuel plan can't run."""
    factory = configured_app["factory"]
    session_id = asyncio.run(_seed_owned_session(factory, clerk_id="caller"))
    bad = {**_VALID_BODY, "planned_fuel_liters": 0.0}
    with TestClient(app) as client:
        resp = _post(client, configured_app, session_id=session_id, body=bad)
    assert resp.status_code == 422


# -- happy path -------------------------------------------------


def test_first_post_returns_201_and_persists(
    configured_app: dict[str, Any],
) -> None:
    """First POST creates the row, returns 201 with the full
    StrategyPlan response shape (server-stamped id + created_at).
    Pin: the persisted state must match the response — a write
    that returns the input echo without persisting is a common
    failure mode."""
    factory = configured_app["factory"]
    session_id = asyncio.run(_seed_owned_session(factory, clerk_id="caller"))
    with TestClient(app) as client:
        resp = _post(
            client, configured_app,
            session_id=session_id, body=_VALID_BODY,
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["session_id"] == str(session_id)
    assert body["planned_stints"] == 3
    assert body["planned_fuel_liters"] == 120.0
    assert body["planned_pit_laps"] == [25, 50]
    assert "id" in body
    assert "created_at" in body

    # Persistence pin: re-read directly.
    async def reread() -> StrategyPlan:
        async with factory() as db:
            return (
                await db.execute(
                    select(StrategyPlan).where(
                        StrategyPlan.session_id == session_id
                    )
                )
            ).scalar_one()

    persisted = asyncio.run(reread())
    assert persisted.planned_stints == 3
    assert persisted.planned_fuel_liters == 120.0
    assert persisted.planned_pit_laps == [25, 50]


# -- duplicate -------------------------------------------------


def test_second_post_on_same_session_returns_409(
    configured_app: dict[str, Any],
) -> None:
    """Second POST on the same session conflicts with the
    UNIQUE(session_id) constraint. Pin: the response must be
    409 (conflict), not a 500 from a raw IntegrityError leak,
    and the original plan must remain unchanged. A re-plan
    during the race goes through StrategyEvent (T097 logs
    kind=plan), not via a second POST here."""
    factory = configured_app["factory"]
    session_id = asyncio.run(_seed_owned_session(factory, clerk_id="caller"))
    with TestClient(app) as client:
        first = _post(
            client, configured_app,
            session_id=session_id, body=_VALID_BODY,
        )
        assert first.status_code == 201
        second = _post(
            client, configured_app,
            session_id=session_id,
            body={**_VALID_BODY, "planned_stints": 99},
        )
    assert second.status_code == 409

    # Original plan must remain unchanged — second POST must NOT
    # have overwritten the first.
    async def reread() -> StrategyPlan:
        async with factory() as db:
            return (
                await db.execute(
                    select(StrategyPlan).where(
                        StrategyPlan.session_id == session_id
                    )
                )
            ).scalar_one()

    persisted = asyncio.run(reread())
    assert persisted.planned_stints == 3  # NOT 99
