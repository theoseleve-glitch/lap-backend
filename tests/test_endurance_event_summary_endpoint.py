"""T075 (sprint2-backend) — GET /api/v1/endurance/events/{event_id}.

Returns an EnduranceEventSummary roll-up: planned event metadata
(from EnduranceEvent T067) + recorded aggregates (sessions linked
via SessionManifest.endurance_event_id T066, plus their StintRecords
T069).

Acceptance:
  - 401 unauthenticated
  - 404 if the event_id doesn't exist
  - 200 with planned shape AND zero aggregates when the event has
    no linked sessions yet (means come back as null, not 0.0)
  - 200 with correct aggregates when sessions + stints exist
    (count + means computed across the linked stints)
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
    EnduranceEvent,
    StintRecord,
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


async def _seed_user(factory: Any, *, clerk_id: str) -> None:
    async with factory() as db:
        db.add(
            User(
                id=uuid6.uuid7(), clerk_user_id=clerk_id,
                email=f"{clerk_id}@t",
            )
        )
        await db.commit()


async def _seed_event_with_stints(
    factory: Any,
    *,
    n_sessions: int,
    stints_per_session: int,
    decay: float,
    fuel: float,
) -> uuid6.UUID:
    """Create an EnduranceEvent, n linked sessions (via manifest),
    and stints on each. Returns the event id."""
    event_id = uuid6.uuid7()
    async with factory() as db:
        event = EnduranceEvent(
            id=event_id,
            event_name="Spa 24",
            total_duration_seconds=86400.0,
            stint_count=12,
            driver_ids=[str(uuid6.uuid7())],
        )
        db.add(event)
        await db.commit()
        # UUID7 is timestamp-prefixed so short hex prefixes collide
        # when two events are seeded within ~6 ms; use the full hex.
        suffix = event_id.hex
        for i in range(n_sessions):
            user = User(
                id=uuid6.uuid7(),
                clerk_user_id=f"d{i}-{suffix}",
                email=f"d{i}-{suffix}@t",
            )
            driver = DriverProfile(
                id=uuid6.uuid7(), user_id=user.id, display_name=f"D{i}",
            )
            session = SessionRow(
                id=uuid6.uuid7(),
                driver_id=driver.id,
                manifest={"endurance_event_id": str(event_id)},
                created_at=datetime(2026, 4, 26, tzinfo=UTC),
                status="processed",
            )
            db.add_all([user, driver, session])
            await db.commit()
            for s_idx in range(1, stints_per_session + 1):
                db.add(
                    StintRecord(
                        id=uuid6.uuid7(),
                        session_id=session.id,
                        stint_index=s_idx,
                        start_lap=1, end_lap=10, lap_count=10,
                        tire_age_laps=0,
                        fuel_used_liters=fuel,
                        lap_time_decay_seconds=decay,
                    )
                )
            await db.commit()
        return event_id


def _get(client: TestClient, ca: dict[str, Any], event_id: str) -> Any:
    return client.get(
        f"/api/v1/endurance/events/{event_id}",
        headers={
            "Authorization": f"Bearer {_bearer(ca['keys'], 'caller')}"
        },
    )


# -- auth ---------------------------------------------------------


def test_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    """No bearer → 401. Pin: the route is auth-gated, never
    publicly readable."""
    with TestClient(app) as client:
        resp = client.get(
            f"/api/v1/endurance/events/{uuid6.uuid7()}"
        )
    assert resp.status_code == 401


# -- not found ----------------------------------------------------


def test_unknown_event_id_returns_404(
    configured_app: dict[str, Any],
) -> None:
    """Event id that doesn't exist → 404. Pin: returning 200
    with empty aggregates would conflate "no such event" with
    "event scheduled but no data" — distinct UI states."""
    asyncio.run(_seed_user(configured_app["factory"], clerk_id="caller"))
    with TestClient(app) as client:
        resp = _get(client, configured_app, str(uuid6.uuid7()))
    assert resp.status_code == 404


# -- empty event --------------------------------------------------


def test_event_with_no_linked_sessions_returns_null_aggregates(
    configured_app: dict[str, Any],
) -> None:
    """Event scheduled, no sessions yet → recorded counts = 0
    AND mean_* = null. Pin: clients distinguish 'no data' from
    a real 0.0 (perfect preservation), so means must be null."""
    factory = configured_app["factory"]
    asyncio.run(_seed_user(factory, clerk_id="caller"))
    event_id = asyncio.run(
        _seed_event_with_stints(
            factory, n_sessions=0, stints_per_session=0,
            decay=0.0, fuel=0.0,
        )
    )
    with TestClient(app) as client:
        resp = _get(client, configured_app, str(event_id))
    assert resp.status_code == 200
    body = resp.json()
    assert body["recorded_session_count"] == 0
    assert body["recorded_stint_count"] == 0
    assert body["mean_lap_time_decay_seconds"] is None
    assert body["mean_fuel_used_liters"] is None


# -- populated event ----------------------------------------------


def test_event_with_recorded_stints_returns_means(
    configured_app: dict[str, Any],
) -> None:
    """Two sessions × three stints with fixed decay/fuel → 6
    stints recorded, means equal the per-row values. Pin the
    aggregation: a switch from mean to sum or first-value would
    fail this exact-equality assertion."""
    factory = configured_app["factory"]
    asyncio.run(_seed_user(factory, clerk_id="caller"))
    event_id = asyncio.run(
        _seed_event_with_stints(
            factory, n_sessions=2, stints_per_session=3,
            decay=0.07, fuel=39.5,
        )
    )
    with TestClient(app) as client:
        resp = _get(client, configured_app, str(event_id))
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(event_id)
    assert body["event_name"] == "Spa 24"
    assert body["recorded_session_count"] == 2
    assert body["recorded_stint_count"] == 6
    assert body["mean_lap_time_decay_seconds"] == pytest.approx(0.07)
    assert body["mean_fuel_used_liters"] == pytest.approx(39.5)


def test_summary_isolates_aggregates_to_target_event(
    configured_app: dict[str, Any],
) -> None:
    """Two events coexist with DIFFERENT decay/fuel values. Asking
    for event A's summary must aggregate only A's stints, not B's.
    Pin the manifest.endurance_event_id filter — without it, the
    aggregate would mix both events' stints and produce a value
    halfway between A and B's per-row constants."""
    factory = configured_app["factory"]
    asyncio.run(_seed_user(factory, clerk_id="caller"))
    event_a = asyncio.run(
        _seed_event_with_stints(
            factory, n_sessions=1, stints_per_session=2,
            decay=0.05, fuel=30.0,
        )
    )
    asyncio.run(
        _seed_event_with_stints(
            factory, n_sessions=1, stints_per_session=2,
            decay=0.50, fuel=80.0,
        )
    )
    with TestClient(app) as client:
        resp = _get(client, configured_app, str(event_a))
    assert resp.status_code == 200
    body = resp.json()
    assert body["recorded_stint_count"] == 2
    # A's per-row decay was 0.05; mixing in B (0.50) would shift
    # the mean to 0.275.
    assert body["mean_lap_time_decay_seconds"] == pytest.approx(0.05)
    assert body["mean_fuel_used_liters"] == pytest.approx(30.0)
