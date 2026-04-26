"""T082 (sprint2-backend) — POST /api/v1/driver/target-events.

Pin an upcoming sim event or real-life session for the
authenticated driver. Request body: TargetEventCreate (no id, no
driver_id — both server-derived). Response: TargetEvent (the
persisted shape, with server-assigned id).

Acceptance:
  - 401 unauthenticated
  - 404 driver_not_found if the user has no DriverProfile yet
  - 422 invalid body (missing required field, competitive_weight
    out of [0, 1], unknown kind, extra field)
  - 201 created: returns full TargetEvent and persists a row that
    round-trips with the supplied fields and server-derived id +
    driver_id
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

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


async def _seed_user_and_driver(
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


async def _seed_user_only(factory: Any, *, clerk_id: str) -> None:
    async with factory() as db:
        db.add(
            User(
                id=uuid6.uuid7(), clerk_user_id=clerk_id,
                email=f"{clerk_id}@t",
            )
        )
        await db.commit()


def _valid_body() -> dict[str, Any]:
    return {
        "name": "iRacing 24h Daytona Qualifier",
        "kind": "sim_event",
        "starts_at": datetime(
            2026, 12, 12, 18, 0, tzinfo=UTC
        ).isoformat(),
        "ends_at": datetime(
            2026, 12, 13, 18, 0, tzinfo=UTC
        ).isoformat(),
        "competitive_weight": 0.85,
        "notes": "Need to nail the bus stop chicane",
    }


def _post(
    client: TestClient, ca: dict[str, Any], body: dict[str, Any],
) -> Any:
    return client.post(
        "/api/v1/driver/target-events",
        headers={
            "Authorization": f"Bearer {_bearer(ca['keys'], 'caller')}"
        },
        json=body,
    )


# -- auth ---------------------------------------------------------


def test_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    """No bearer → 401. Pin: the route is auth-gated; pinning a
    target on a driver's profile must require authentication."""
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/driver/target-events", json=_valid_body()
        )
    assert resp.status_code == 401


# -- driver not found --------------------------------------------


def test_user_without_driver_profile_returns_404(
    configured_app: dict[str, Any],
) -> None:
    """Authenticated user but no DriverProfile yet → 404. Pin:
    we surface the missing-profile state explicitly rather than
    auto-creating one (driver creation is a separate flow)."""
    asyncio.run(
        _seed_user_only(configured_app["factory"], clerk_id="caller")
    )
    with TestClient(app) as client:
        resp = _post(client, configured_app, _valid_body())
    assert resp.status_code == 404
    # Pin the business reason — without this, a missing route would
    # also return 404 and silently satisfy the test.
    assert resp.json().get("detail") == "driver_not_found"


# -- validation ---------------------------------------------------


def test_competitive_weight_above_one_returns_422(
    configured_app: dict[str, Any],
) -> None:
    """competitive_weight must be in [0, 1] per ADR §5.7. Pin: a
    DB-level CHECK would 500 here, but Pydantic gives a useful
    422 — the validator is what actually enforces the contract."""
    asyncio.run(
        _seed_user_and_driver(
            configured_app["factory"], clerk_id="caller"
        )
    )
    body = _valid_body()
    body["competitive_weight"] = 1.7
    with TestClient(app) as client:
        resp = _post(client, configured_app, body)
    assert resp.status_code == 422


def test_unknown_kind_returns_422(
    configured_app: dict[str, Any],
) -> None:
    """kind is a closed Literal of 4 values. Pin: an unknown kind
    must 422 (not 201) — downstream rules switch on this string."""
    asyncio.run(
        _seed_user_and_driver(
            configured_app["factory"], clerk_id="caller"
        )
    )
    body = _valid_body()
    body["kind"] = "esports_league"
    with TestClient(app) as client:
        resp = _post(client, configured_app, body)
    assert resp.status_code == 422


def test_unknown_field_returns_422(
    configured_app: dict[str, Any],
) -> None:
    """extra='forbid' on TargetEventCreate: a typoed field name
    (e.g., "starts" instead of "starts_at") must be rejected
    rather than silently dropped — silent drops shipped a real
    bug in the past where event_name was lost on POST."""
    asyncio.run(
        _seed_user_and_driver(
            configured_app["factory"], clerk_id="caller"
        )
    )
    body = _valid_body()
    body["mystery_field"] = "oops"
    with TestClient(app) as client:
        resp = _post(client, configured_app, body)
    assert resp.status_code == 422


# -- happy path ---------------------------------------------------


def test_valid_post_creates_target_event_and_returns_201(
    configured_app: dict[str, Any],
) -> None:
    """Valid body + authenticated user with driver profile → 201
    with full TargetEvent body (server-assigned id), and a
    persisted row whose driver_id matches the caller's profile.

    Pin: id must be server-generated (uuid6.uuid7), driver_id
    must come from the auth context, and every supplied field
    must round-trip through the persistence layer untouched.
    """
    factory = configured_app["factory"]
    driver_id = asyncio.run(
        _seed_user_and_driver(factory, clerk_id="caller")
    )
    with TestClient(app) as client:
        resp = _post(client, configured_app, _valid_body())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "iRacing 24h Daytona Qualifier"
    assert body["kind"] == "sim_event"
    assert body["competitive_weight"] == 0.85
    assert body["notes"] == "Need to nail the bus stop chicane"
    # Server-assigned id must be a valid UUID.
    returned_id = UUID(body["id"])

    async def load_persisted() -> TargetEvent:
        async with factory() as db:
            return (
                await db.execute(select(TargetEvent))
            ).scalar_one()

    persisted = asyncio.run(load_persisted())
    assert persisted.id == returned_id
    assert persisted.driver_id == driver_id
    assert persisted.name == "iRacing 24h Daytona Qualifier"
    assert persisted.kind == "sim_event"
    assert persisted.competitive_weight == 0.85


def test_optional_fields_omitted_persist_as_none(
    configured_app: dict[str, Any],
) -> None:
    """A real-life event whose track isn't in the catalog yet:
    omit ends_at/track_id/car_id/notes, supply only the required
    fields. Pin: the request schema's Optional[]=None defaults
    must reach the DB as NULL, not as an empty string or zero."""
    factory = configured_app["factory"]
    asyncio.run(_seed_user_and_driver(factory, clerk_id="caller"))
    minimal_body = {
        "name": "Local karting test",
        "kind": "real_test",
        "starts_at": datetime(
            2026, 6, 14, 9, 0, tzinfo=UTC
        ).isoformat(),
        "competitive_weight": 0.2,
    }
    with TestClient(app) as client:
        resp = _post(client, configured_app, minimal_body)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["ends_at"] is None
    assert body["track_id"] is None
    assert body["car_id"] is None
    assert body["notes"] is None
