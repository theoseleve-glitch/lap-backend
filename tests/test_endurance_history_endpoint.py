"""T076 (sprint2-backend) — GET /api/v1/driver/endurance/history.

Returns the EnduranceEvents the authenticated driver is entered in.
Membership taken from EnduranceEvent.driver_ids (T067 explicit
roster), not session-derived — pre-event entries appear before any
session is logged.

Acceptance:
  - 401 unauthenticated
  - 404 if the auth user has no DriverProfile (mirror T053/T058)
  - 200 with empty events list when the driver isn't on any roster
  - 200 with one entry per matching event when the driver IS on
    one or more rosters
  - cross-driver isolation: another driver's events do NOT appear
"""

from __future__ import annotations

import asyncio
import base64
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
from infra.orm import DriverProfile, EnduranceEvent, User

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


async def _seed_user_and_driver(
    factory: Any, *, clerk_id: str
) -> uuid6.UUID:
    """Create a User + DriverProfile and return the driver id."""
    async with factory() as db:
        user = User(
            id=uuid6.uuid7(), clerk_user_id=clerk_id,
            email=f"{clerk_id}@t",
        )
        driver = DriverProfile(
            id=uuid6.uuid7(), user_id=user.id, display_name="D",
        )
        db.add_all([user, driver])
        await db.commit()
        return driver.id


async def _seed_event(
    factory: Any, *, name: str, driver_ids: list[uuid6.UUID],
) -> uuid6.UUID:
    """Create an EnduranceEvent with the given driver roster."""
    eid = uuid6.uuid7()
    async with factory() as db:
        db.add(
            EnduranceEvent(
                id=eid,
                event_name=name,
                total_duration_seconds=86400.0,
                stint_count=12,
                driver_ids=[str(d) for d in driver_ids],
            )
        )
        await db.commit()
    return eid


def _get(client: TestClient, ca: dict[str, Any], who: str) -> Any:
    return client.get(
        "/api/v1/driver/endurance/history",
        headers={"Authorization": f"Bearer {_bearer(ca['keys'], who)}"},
    )


# -- auth ---------------------------------------------------------


def test_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    """No bearer → 401. Pin: the route is auth-gated."""
    with TestClient(app) as client:
        resp = client.get("/api/v1/driver/endurance/history")
    assert resp.status_code == 401


# -- 404 when no driver profile -----------------------------------


def test_no_driver_profile_returns_404(
    configured_app: dict[str, Any],
) -> None:
    """Auth'd user with no DriverProfile → 404. Mirrors T053/T058
    behavior — the endpoint pivots on driver_id, not user_id."""
    factory = configured_app["factory"]
    async def _seed() -> None:
        async with factory() as db:
            db.add(
                User(
                    id=uuid6.uuid7(), clerk_user_id="orphan",
                    email="orphan@t",
                )
            )
            await db.commit()
    asyncio.run(_seed())
    with TestClient(app) as client:
        resp = _get(client, configured_app, "orphan")
    assert resp.status_code == 404


# -- empty history ------------------------------------------------


def test_driver_with_no_events_returns_empty_list(
    configured_app: dict[str, Any],
) -> None:
    """A driver who has never been entered in an endurance event
    sees `events: []`, not 404 — empty list is the sentinel for
    'no history'."""
    factory = configured_app["factory"]
    asyncio.run(_seed_user_and_driver(factory, clerk_id="alice"))
    with TestClient(app) as client:
        resp = _get(client, configured_app, "alice")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"events": []}


# -- driver on one roster -----------------------------------------


def test_driver_on_one_roster_returns_that_event(
    configured_app: dict[str, Any],
) -> None:
    """One event with the driver in its roster → list contains
    one entry with the event's planned-shape fields."""
    factory = configured_app["factory"]
    driver_id = asyncio.run(
        _seed_user_and_driver(factory, clerk_id="bob")
    )
    event_id = asyncio.run(
        _seed_event(factory, name="Spa 24", driver_ids=[driver_id])
    )
    with TestClient(app) as client:
        resp = _get(client, configured_app, "bob")
    body = resp.json()
    assert len(body["events"]) == 1
    entry = body["events"][0]
    assert entry["id"] == str(event_id)
    assert entry["event_name"] == "Spa 24"
    assert entry["stint_count"] == 12
    assert entry["total_duration_seconds"] == 86400.0


# -- cross-driver isolation ---------------------------------------


def test_driver_does_not_see_other_drivers_events(
    configured_app: dict[str, Any],
) -> None:
    """Bob is on event A's roster; Alice is on event B's. Asking
    for Bob's history must return only A — never B. Pin the
    membership filter; without it, every endurance event leaks
    to every authenticated driver."""
    factory = configured_app["factory"]
    bob_id = asyncio.run(
        _seed_user_and_driver(factory, clerk_id="bob")
    )
    alice_id = asyncio.run(
        _seed_user_and_driver(factory, clerk_id="alice")
    )
    event_a = asyncio.run(
        _seed_event(factory, name="A", driver_ids=[bob_id])
    )
    event_b = asyncio.run(
        _seed_event(factory, name="B", driver_ids=[alice_id])
    )
    with TestClient(app) as client:
        resp = _get(client, configured_app, "bob")
    body = resp.json()
    ids = {e["id"] for e in body["events"]}
    assert str(event_a) in ids
    assert str(event_b) not in ids
