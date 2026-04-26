"""T084 (sprint2-backend) — GET /api/v1/driver/target-events.

Driver-scoped list of pinned upcoming events. Returns a
TargetEventList wrapping tuple[TargetEvent, ...] sorted by
starts_at ASC (soonest first).

Acceptance:
  - 401 unauthenticated
  - 404 driver_not_found
  - 200 with empty target_events when the driver has no pins
  - 200 with ONLY the caller's pins (ownership scoping; another
    driver's pins must NOT appear — same authz pin as T083)
  - 200 entries sorted by starts_at ASC (soonest first)
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


async def _seed_driver(
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
        "/api/v1/driver/target-events",
        headers={"Authorization": f"Bearer {_bearer(ca['keys'], clerk_id)}"},
    )


# -- auth ---------------------------------------------------------


def test_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    """No bearer → 401. Pin: the list is auth-gated; one driver
    must never see another driver's pins."""
    with TestClient(app) as client:
        resp = client.get("/api/v1/driver/target-events")
    assert resp.status_code == 401


# -- driver not found --------------------------------------------


def test_user_without_driver_profile_returns_404(
    configured_app: dict[str, Any],
) -> None:
    """Authenticated user but no DriverProfile yet → 404. Pin:
    surface missing-profile explicitly with a stable detail, so
    a missing-route 404 can't masquerade as a successful list
    of zero pins (which would be 200 with []).
    """
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


# -- empty list ---------------------------------------------------


def test_driver_with_no_pins_returns_empty_list(
    configured_app: dict[str, Any],
) -> None:
    """Driver exists but has pinned no events → 200 with empty
    target_events array. Pin: empty must NOT be a 404 — distinct
    from "driver doesn't exist", and the client UI has different
    states for the two."""
    asyncio.run(_seed_driver(configured_app["factory"], clerk_id="caller"))
    with TestClient(app) as client:
        resp = _get(client, configured_app)
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"target_events": []}


# -- ownership scoping --------------------------------------------


def test_list_returns_only_callers_pins(
    configured_app: dict[str, Any],
) -> None:
    """Caller has 2 pins, another driver has 1. The list must
    contain exactly the caller's 2; the third must not appear.
    Pin: this is the data-leak authz check — a missing
    driver_id WHERE clause would return all 3 rows and expose
    other drivers' pinned events."""
    factory = configured_app["factory"]
    caller_id = asyncio.run(_seed_driver(factory, clerk_id="caller"))
    other_id = asyncio.run(_seed_driver(factory, clerk_id="other"))
    pin_a = asyncio.run(
        _seed_pin(
            factory, driver_id=caller_id, name="caller A",
            starts_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
    )
    pin_b = asyncio.run(
        _seed_pin(
            factory, driver_id=caller_id, name="caller B",
            starts_at=datetime(2026, 9, 1, tzinfo=UTC),
        )
    )
    asyncio.run(
        _seed_pin(
            factory, driver_id=other_id, name="OTHER ONLY",
            starts_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
    )
    with TestClient(app) as client:
        resp = _get(client, configured_app)
    assert resp.status_code == 200
    body = resp.json()
    returned_ids = {e["id"] for e in body["target_events"]}
    assert returned_ids == {str(pin_a), str(pin_b)}
    returned_names = {e["name"] for e in body["target_events"]}
    assert "OTHER ONLY" not in returned_names


# -- sort order ---------------------------------------------------


def test_list_is_sorted_by_starts_at_ascending(
    configured_app: dict[str, Any],
) -> None:
    """Three pins inserted in non-chronological order must come
    back sorted by starts_at ASC (soonest first). Pin: the UI
    displays "your next event" by reading list[0], so order is
    semantic — a regression to insertion order or DESC would
    surface the wrong event there."""
    factory = configured_app["factory"]
    caller_id = asyncio.run(_seed_driver(factory, clerk_id="caller"))
    # Seed in scrambled order: middle, latest, earliest.
    asyncio.run(
        _seed_pin(
            factory, driver_id=caller_id, name="middle",
            starts_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
    )
    asyncio.run(
        _seed_pin(
            factory, driver_id=caller_id, name="latest",
            starts_at=datetime(2026, 9, 1, tzinfo=UTC),
        )
    )
    asyncio.run(
        _seed_pin(
            factory, driver_id=caller_id, name="earliest",
            starts_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
    )
    with TestClient(app) as client:
        resp = _get(client, configured_app)
    assert resp.status_code == 200
    body = resp.json()
    names_in_order = [e["name"] for e in body["target_events"]]
    assert names_in_order == ["earliest", "middle", "latest"]
