"""T093 (sprint2-backend) — GET /api/v1/driver/target-events/countdown.

Server-side countdown to the driver's soonest UPCOMING pinned
TargetEvent. Why server-side: server clock is authoritative
(client clocks drift, especially on mobile in the final hours
before an event). The endpoint also folds in derived helpers
(days/hours rounded) so the UI doesn't reimplement date math.

Acceptance:
  - 401 unauthenticated
  - 404 when caller has no DriverProfile
  - 200 with target_event=None when driver has no upcoming pin
    (this is distinct from 404 — the driver exists, just hasn't
    pinned anything; UI states for the two cases differ)
  - 200 with the SOONEST upcoming pin when multiple are pinned
  - 200 ignoring past-only pins (countdown is forward-looking;
    surface "events you've already done" via T084's list, not
    the countdown)
  - delta fields (seconds_until_start, days_until_start) are
    consistent with each other and with target_event.starts_at
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
    TargetEvent,
    User,
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


async def _seed_driver(factory: Any, *, clerk_id: str) -> uuid6.UUID:
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


async def _seed_pin(
    factory: Any,
    *,
    driver_id: uuid6.UUID,
    name: str,
    starts_at: datetime,
) -> uuid6.UUID:
    async with factory() as db:
        event_id = uuid6.uuid7()
        db.add(
            TargetEvent(
                id=event_id,
                driver_id=driver_id,
                name=name,
                kind="sim_event",
                starts_at=starts_at,
                competitive_weight=0.5,
            )
        )
        await db.commit()
        return event_id


def _get(
    client: TestClient, ca: dict[str, Any], *, clerk_id: str = "caller",
) -> Any:
    return client.get(
        "/api/v1/driver/target-events/countdown",
        headers={"Authorization": f"Bearer {_bearer(ca['keys'], clerk_id)}"},
    )


# -- auth --------------------------------------------------------


def test_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    """No bearer → 401. Pin: countdown leaks "do you have an
    upcoming event" so it must be auth-gated."""
    with TestClient(app) as client:
        resp = client.get("/api/v1/driver/target-events/countdown")
    assert resp.status_code == 401


# -- driver not found -------------------------------------------


def test_user_without_driver_profile_returns_404(
    configured_app: dict[str, Any],
) -> None:
    """Authenticated user but no DriverProfile yet → 404. Pin:
    distinct from 200-with-None-event; the UI shows "complete
    your profile" instead of "no upcoming event"."""
    factory = configured_app["factory"]
    async def seed_user_only() -> None:
        async with factory() as db:
            db.add(
                User(
                    id=uuid6.uuid7(), clerk_user_id="caller",
                    email="caller@t",
                )
            )
            await db.commit()
    asyncio.run(seed_user_only())
    with TestClient(app) as client:
        resp = _get(client, configured_app)
    assert resp.status_code == 404
    assert resp.json().get("detail") == "driver_not_found"


# -- no upcoming pin --------------------------------------------


def test_no_pins_returns_target_event_null(
    configured_app: dict[str, Any],
) -> None:
    """Driver exists but has zero pins → 200 with target_event
    null and both delta fields null. Pin: must NOT be a 404 —
    "you have no event scheduled" is a legitimate UI state."""
    asyncio.run(_seed_driver(configured_app["factory"], clerk_id="caller"))
    with TestClient(app) as client:
        resp = _get(client, configured_app)
    assert resp.status_code == 200
    body = resp.json()
    assert body["target_event"] is None
    assert body["seconds_until_start"] is None
    assert body["days_until_start"] is None
    assert "server_now" in body  # always echoed for drift detection


def test_only_past_pins_returns_target_event_null(
    configured_app: dict[str, Any],
) -> None:
    """Driver only has events whose starts_at is in the past →
    same shape as no-pins (target_event null). Pin: countdown
    is forward-looking; a regression returning the most-recent
    past event would coach the driver to "prep for an event
    that already happened"."""
    factory = configured_app["factory"]
    caller_id = asyncio.run(_seed_driver(factory, clerk_id="caller"))
    long_ago = datetime.now(UTC) - timedelta(days=30)
    asyncio.run(
        _seed_pin(
            factory, driver_id=caller_id, name="last month's race",
            starts_at=long_ago,
        )
    )
    with TestClient(app) as client:
        resp = _get(client, configured_app)
    assert resp.status_code == 200
    assert resp.json()["target_event"] is None


# -- soonest upcoming -------------------------------------------


def test_returns_soonest_upcoming_when_multiple_pins(
    configured_app: dict[str, Any],
) -> None:
    """Three pins: past + later-future + sooner-future. The
    sooner-future one wins. Pin: soonest is the UI's "your next
    event" semantic; sorting by competitive_weight or insertion
    order would surface the wrong one."""
    factory = configured_app["factory"]
    caller_id = asyncio.run(_seed_driver(factory, clerk_id="caller"))
    now = datetime.now(UTC)
    # Past — must be skipped.
    asyncio.run(
        _seed_pin(
            factory, driver_id=caller_id, name="past",
            starts_at=now - timedelta(days=10),
        )
    )
    # Later future.
    asyncio.run(
        _seed_pin(
            factory, driver_id=caller_id, name="later",
            starts_at=now + timedelta(days=60),
        )
    )
    # Sooner future — should be returned.
    sooner_id = asyncio.run(
        _seed_pin(
            factory, driver_id=caller_id, name="sooner",
            starts_at=now + timedelta(days=7),
        )
    )
    with TestClient(app) as client:
        resp = _get(client, configured_app)
    assert resp.status_code == 200
    body = resp.json()
    assert body["target_event"]["id"] == str(sooner_id)
    assert body["target_event"]["name"] == "sooner"


# -- delta correctness ------------------------------------------


def test_delta_fields_match_target_event_starts_at(
    configured_app: dict[str, Any],
) -> None:
    """seconds_until_start must equal (starts_at - server_now)
    seconds (within tolerance). days_until_start must equal
    floor(seconds_until_start / 86400). Pin: a regression that
    computed the deltas off a stale "now" or off the wrong
    field would surface a mismatched countdown — the UI's
    "5 days left" header would disagree with its starts_at
    timestamp."""
    factory = configured_app["factory"]
    caller_id = asyncio.run(_seed_driver(factory, clerk_id="caller"))
    starts_at = datetime.now(UTC) + timedelta(days=10, hours=3)
    asyncio.run(
        _seed_pin(
            factory, driver_id=caller_id, name="t093 race",
            starts_at=starts_at,
        )
    )
    with TestClient(app) as client:
        resp = _get(client, configured_app)
    assert resp.status_code == 200
    body = resp.json()
    server_now = datetime.fromisoformat(body["server_now"])
    secs = body["seconds_until_start"]
    expected = int((starts_at - server_now).total_seconds())
    # Tolerance: same-process latency is negligible but the
    # server may stamp `now` a moment after the test stamps
    # `starts_at`. Allow ±2s — anything wider would let a stale
    # delta slip through.
    assert abs(secs - expected) <= 2, (
        f"delta drift: returned={secs}, expected≈{expected}"
    )
    # Days is the floor, not rounded — 10d3h must read as 10,
    # not 10.125 or 11.
    assert body["days_until_start"] == secs // 86400


def test_server_now_is_recent(
    configured_app: dict[str, Any],
) -> None:
    """server_now is fresh (stamped at request time, not at
    process start). Pin: a regression that cached server_now
    at module-load would silently break drift detection — the
    field is only useful as live ground truth."""
    asyncio.run(_seed_driver(configured_app["factory"], clerk_id="caller"))
    before = datetime.now(UTC)
    with TestClient(app) as client:
        resp = _get(client, configured_app)
    after = datetime.now(UTC)
    assert resp.status_code == 200
    server_now = datetime.fromisoformat(resp.json()["server_now"])
    assert before - timedelta(seconds=2) <= server_now <= after + timedelta(
        seconds=2
    )
