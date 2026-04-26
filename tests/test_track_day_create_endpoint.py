"""T088 (sprint2-backend) — POST /api/v1/driver/track-days.

Manual real-life event add. UX-friendly wrapper that takes a
free-text venue_name (the venue isn't in our iRacing-sourced
catalog) and persists a TargetEvent with kind="real_track_day".

Why a separate endpoint instead of just documenting "use POST
/target-events with kind=real_track_day": the existing endpoint
requires the caller to supply `kind` as a Literal string and to
either provide a track_id (catalog FK) or explicitly leave it
None. The mental model is "schema-modeling a target event." The
track-days endpoint is "I'm doing a track day Saturday" — the
verb the user thinks in. Both paths land in the same
target_events table; downstream consumers see one storage shape.

Acceptance:
  - 401 unauthenticated
  - 404 driver_not_found if user has no DriverProfile
  - 422 venue_name missing (the only required-beyond-defaults
    field — without it the row is just a date with no context)
  - 422 venue_name empty string (min_length=1)
  - 422 competitive_weight out of [0, 1]
  - 201 happy path: returns full TargetEvent with kind auto-set,
    venue_name folded into the event name (so the GET list
    surfaces it without a separate venue column), and the row
    persists scoped to the caller's driver_id.
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
        "venue_name": "Castle Combe Circuit",
        "starts_at": datetime(
            2026, 6, 14, 9, 0, tzinfo=UTC
        ).isoformat(),
        "ends_at": datetime(
            2026, 6, 14, 17, 0, tzinfo=UTC
        ).isoformat(),
        "competitive_weight": 0.4,
        "notes": "Bring rain tires",
    }


def _post(
    client: TestClient, ca: dict[str, Any], body: dict[str, Any],
) -> Any:
    return client.post(
        "/api/v1/driver/track-days",
        headers={
            "Authorization": f"Bearer {_bearer(ca['keys'], 'caller')}"
        },
        json=body,
    )


# -- auth ---------------------------------------------------------


def test_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    """No bearer → 401. Pin: track-day add is auth-gated; the
    persisted TargetEvent must belong to a real driver."""
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/driver/track-days", json=_valid_body()
        )
    assert resp.status_code == 401


# -- driver not found --------------------------------------------


def test_user_without_driver_profile_returns_404(
    configured_app: dict[str, Any],
) -> None:
    """Authenticated user without a DriverProfile → 404. Pin
    detail string so missing-route 404 can't masquerade."""
    asyncio.run(
        _seed_user_only(configured_app["factory"], clerk_id="caller")
    )
    with TestClient(app) as client:
        resp = _post(client, configured_app, _valid_body())
    assert resp.status_code == 404
    assert resp.json().get("detail") == "driver_not_found"


# -- validation ---------------------------------------------------


def test_missing_venue_name_returns_422(
    configured_app: dict[str, Any],
) -> None:
    """venue_name is the only required-beyond-defaults field
    here — without it the entry is just a date with no context.
    Pin: missing venue must 422 (not silently default to "")."""
    asyncio.run(_seed_driver(configured_app["factory"], clerk_id="caller"))
    body = _valid_body()
    del body["venue_name"]
    with TestClient(app) as client:
        resp = _post(client, configured_app, body)
    assert resp.status_code == 422


def test_empty_venue_name_returns_422(
    configured_app: dict[str, Any],
) -> None:
    """Empty string is also invalid (min_length=1). Pin: a
    whitespace-stripping regression that allowed empty strings
    would let users submit unidentifiable rows."""
    asyncio.run(_seed_driver(configured_app["factory"], clerk_id="caller"))
    body = _valid_body()
    body["venue_name"] = ""
    with TestClient(app) as client:
        resp = _post(client, configured_app, body)
    assert resp.status_code == 422


def test_competitive_weight_out_of_range_returns_422(
    configured_app: dict[str, Any],
) -> None:
    """Same [0, 1] bound as TargetEvent; pin the validation
    against the schema-level guard since DB has no CHECK."""
    asyncio.run(_seed_driver(configured_app["factory"], clerk_id="caller"))
    body = _valid_body()
    body["competitive_weight"] = 1.5
    with TestClient(app) as client:
        resp = _post(client, configured_app, body)
    assert resp.status_code == 422


# -- happy path ---------------------------------------------------


def test_valid_post_creates_target_event_with_real_track_day_kind(
    configured_app: dict[str, Any],
) -> None:
    """Valid body → 201 with full TargetEvent body, kind
    auto-set to "real_track_day", venue_name folded into the
    persisted event name (so the GET list surfaces it without
    a separate venue column on TargetEvent), driver_id
    matches the caller. track_id and car_id stay None."""
    factory = configured_app["factory"]
    driver_id = asyncio.run(
        _seed_driver(factory, clerk_id="caller")
    )
    with TestClient(app) as client:
        resp = _post(client, configured_app, _valid_body())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "real_track_day"
    assert "Castle Combe Circuit" in body["name"], (
        "venue_name must be visible in the event name so the "
        "GET list shows it without a dedicated venue column"
    )
    assert body["track_id"] is None
    assert body["car_id"] is None
    assert body["competitive_weight"] == 0.4
    assert body["notes"] == "Bring rain tires"

    async def load_persisted() -> TargetEvent:
        async with factory() as db:
            return (
                await db.execute(select(TargetEvent))
            ).scalar_one()

    persisted = asyncio.run(load_persisted())
    assert persisted.driver_id == driver_id
    assert persisted.kind == "real_track_day"
    assert "Castle Combe Circuit" in persisted.name


def test_minimal_body_uses_defaults_and_persists(
    configured_app: dict[str, Any],
) -> None:
    """Minimum viable body: just venue_name + starts_at. Pin
    competitive_weight default (0.3 — track days are
    skill-building, not full-stakes) and that ends_at/notes
    default to None on the persisted row."""
    factory = configured_app["factory"]
    asyncio.run(_seed_driver(factory, clerk_id="caller"))
    minimal_body = {
        "venue_name": "Local kart track",
        "starts_at": datetime(
            2026, 6, 14, 9, 0, tzinfo=UTC
        ).isoformat(),
    }
    with TestClient(app) as client:
        resp = _post(client, configured_app, minimal_body)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["competitive_weight"] == 0.3, (
        "default must be 0.3 — pin the schema-level default"
    )
    assert body["ends_at"] is None
    assert body["notes"] is None
