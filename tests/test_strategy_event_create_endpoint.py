"""T099 (sprint2-backend) — POST /api/v1/sessions/{id}/strategy/events.

Append-only writer for the runtime strategy timeline. Each
POST creates one StrategyEvent row (T097 ORM): plan / armed /
pit / stint_end / fcy / sc, with a lap number and an optional
kind-specific details payload.

Acceptance:
  - 401 unauthenticated
  - 404 when session doesn't exist OR caller doesn't own it
    (status parity to avoid enumeration; same pattern as T098)
  - 422 on invalid body (bad kind, negative at_lap)
  - 201 with the persisted StrategyEvent on each call
  - multiple POSTs APPEND (no uniqueness constraint blocking
    second event of same kind on same lap — chaotic-race
    legitimate scenarios per T097 design)
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
            id=uuid6.uuid7(), clerk_user_id="other099",
            email="other099@t",
        )
        driver = DriverProfile(
            id=uuid6.uuid7(), user_id=user.id, display_name="other099",
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
        f"/api/v1/sessions/{session_id}/strategy/events",
        headers={
            "Authorization": f"Bearer {_bearer(ca['keys'], clerk_id)}"
        },
        json=body,
    )


_VALID_PIT: dict[str, Any] = {
    "kind": "pit",
    "at_lap": 25,
    "details": {"stint_index": 1, "fuel_added_liters": 40.0},
}


# -- auth --------------------------------------------------------


def test_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    """No bearer → 401. Pin: a session's runtime timeline is
    owner-private; an unauthenticated POST must never persist."""
    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/sessions/{uuid6.uuid7()}/strategy/events",
            json=_VALID_PIT,
        )
    assert resp.status_code == 401


# -- ownership scoping ------------------------------------------


def test_post_to_other_drivers_session_returns_404(
    configured_app: dict[str, Any],
) -> None:
    """Caller authed but session belongs to another driver. Pin:
    same 404 as "not found" — no enumeration oracle."""
    factory = configured_app["factory"]
    asyncio.run(_seed_owned_session(factory, clerk_id="caller"))
    other_session_id = asyncio.run(_seed_other_drivers_session(factory))
    with TestClient(app) as client:
        resp = _post(
            client, configured_app,
            session_id=other_session_id, body=_VALID_PIT,
        )
    assert resp.status_code == 404


def test_post_to_unknown_session_returns_404(
    configured_app: dict[str, Any],
) -> None:
    asyncio.run(_seed_owned_session(configured_app["factory"], clerk_id="caller"))
    with TestClient(app) as client:
        resp = _post(
            client, configured_app,
            session_id=uuid6.uuid7(), body=_VALID_PIT,
        )
    assert resp.status_code == 404


# -- validation -------------------------------------------------


def test_invalid_kind_returns_422(
    configured_app: dict[str, Any],
) -> None:
    """`kind` is a Literal of the spec set; an unrecognized
    value (e.g., a typo "ftl") must surface as a useful 422,
    not a 500. Pin: enforces the Pydantic-side decision over
    a DB CHECK (T097's note)."""
    factory = configured_app["factory"]
    session_id = asyncio.run(_seed_owned_session(factory, clerk_id="caller"))
    bad = {**_VALID_PIT, "kind": "ftl"}
    with TestClient(app) as client:
        resp = _post(client, configured_app, session_id=session_id, body=bad)
    assert resp.status_code == 422


def test_negative_at_lap_returns_422(
    configured_app: dict[str, Any],
) -> None:
    """at_lap must be >= 0; lap -1 is meaningless. Pin: bounds
    enforcement in lap-schemas, not at the DB layer."""
    factory = configured_app["factory"]
    session_id = asyncio.run(_seed_owned_session(factory, clerk_id="caller"))
    bad = {**_VALID_PIT, "at_lap": -1}
    with TestClient(app) as client:
        resp = _post(client, configured_app, session_id=session_id, body=bad)
    assert resp.status_code == 422


# -- happy path -------------------------------------------------


def test_post_creates_and_persists_event(
    configured_app: dict[str, Any],
) -> None:
    """Single POST creates the row, returns 201 with the full
    StrategyEvent response (server-stamped id + created_at).
    Persistence pin: re-read directly to confirm the write."""
    factory = configured_app["factory"]
    session_id = asyncio.run(_seed_owned_session(factory, clerk_id="caller"))
    with TestClient(app) as client:
        resp = _post(
            client, configured_app,
            session_id=session_id, body=_VALID_PIT,
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["session_id"] == str(session_id)
    assert body["kind"] == "pit"
    assert body["at_lap"] == 25
    assert body["details"] == {
        "stint_index": 1, "fuel_added_liters": 40.0,
    }
    assert "id" in body
    assert "created_at" in body

    async def reread() -> StrategyEvent:
        async with factory() as db:
            return (
                await db.execute(
                    select(StrategyEvent).where(
                        StrategyEvent.session_id == session_id
                    )
                )
            ).scalar_one()

    persisted = asyncio.run(reread())
    assert persisted.kind == "pit"
    assert persisted.at_lap == 25
    assert persisted.details == {
        "stint_index": 1, "fuel_added_liters": 40.0,
    }


# -- append-only (no uniqueness blocking duplicates) ------------


def test_two_events_same_kind_same_lap_both_persist(
    configured_app: dict[str, Any],
) -> None:
    """Two FCY events on the same lap (chaotic-race scenario
    per T097 design) must both be accepted. Pin: rejecting the
    second would lose legitimate timeline data — append-only
    semantics are the contract."""
    factory = configured_app["factory"]
    session_id = asyncio.run(_seed_owned_session(factory, clerk_id="caller"))
    fcy = {"kind": "fcy", "at_lap": 5, "details": {}}
    with TestClient(app) as client:
        first = _post(
            client, configured_app,
            session_id=session_id, body=fcy,
        )
        second = _post(
            client, configured_app,
            session_id=session_id, body=fcy,
        )
    assert first.status_code == 201
    assert second.status_code == 201

    async def count() -> int:
        async with factory() as db:
            rows = (
                await db.execute(
                    select(StrategyEvent).where(
                        StrategyEvent.session_id == session_id
                    )
                )
            ).scalars().all()
            return len(rows)

    assert asyncio.run(count()) == 2
