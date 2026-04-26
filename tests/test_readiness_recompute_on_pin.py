"""T089 (sprint2-backend) — pin/unpin → readiness recompute hook.

Tests that the pin endpoints (T082 POST /target-events, T088 POST
/track-days, T083 DELETE /target-events/:id) call
recompute_driver_readiness after committing the pin/unpin so a
DriverReadinessState snapshot lands in the same request cycle.

Pure-domain readiness behavior is covered in
test_readiness_recompute.py. This file pins ONLY the wiring —
that the endpoints actually invoke the hook.

Acceptance:
  - POST /target-events → exactly one new DriverReadinessState
    row per request, target_event reflects the just-pinned event
  - DELETE /target-events/:id → one new readiness row, latest
    target reflects the remaining pin (or None if last pin
    deleted)
  - POST /track-days (T088) → also triggers recompute, target
    reflects the new track day
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
from sqlalchemy import select

from alembic import command
from app.deps import get_db, get_jwks_client
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import (
    DriverProfile,
    DriverReadinessState,
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


async def _readiness_rows(
    factory: Any, driver_id: uuid6.UUID,
) -> list[DriverReadinessState]:
    async with factory() as db:
        return list(
            (
                await db.execute(
                    select(DriverReadinessState)
                    .where(DriverReadinessState.driver_id == driver_id)
                    .order_by(DriverReadinessState.computed_at.asc())
                )
            ).scalars().all()
        )


def _bearer_header(ca: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {_bearer(ca['keys'], 'caller')}"}


def _target_body() -> dict[str, Any]:
    return {
        "name": "Spa 6h Endurance",
        "kind": "sim_event",
        "starts_at": (
            datetime.now(UTC) + timedelta(days=14)
        ).isoformat(),
        "competitive_weight": 0.85,
    }


# -- pin via POST /target-events ---------------------------------


def test_pinning_target_event_appends_one_readiness_snapshot(
    configured_app: dict[str, Any],
) -> None:
    """POST /target-events → exactly one new DriverReadinessState
    row, with target_event JSON reflecting the just-pinned event.
    Pin: a missing hook would leave readiness stale (no snapshot),
    breaking T090's plan-generation read path."""
    factory = configured_app["factory"]
    driver_id = asyncio.run(
        _seed_driver(factory, clerk_id="caller")
    )
    body = _target_body()
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/driver/target-events",
            headers=_bearer_header(configured_app),
            json=body,
        )
    assert resp.status_code == 201, resp.text
    rows = asyncio.run(_readiness_rows(factory, driver_id))
    assert len(rows) == 1, (
        f"expected 1 readiness snapshot after pin, got {len(rows)}"
    )
    assert rows[0].target_event is not None
    assert rows[0].target_event["name"] == "Spa 6h Endurance"
    assert rows[0].event_pressure_input == 0.85, (
        "competitive_weight from the pinned event must surface "
        "as event_pressure_input on the readiness snapshot"
    )


# -- unpin via DELETE --------------------------------------------


def test_unpinning_target_event_appends_snapshot_with_null_target(
    configured_app: dict[str, Any],
) -> None:
    """Pin then unpin → 2 readiness snapshots; the second has
    target_event=None (no remaining pins). Pin: the unpin hook
    must fire too, otherwise readiness reflects the deleted
    target indefinitely."""
    factory = configured_app["factory"]
    driver_id = asyncio.run(
        _seed_driver(factory, clerk_id="caller")
    )
    with TestClient(app) as client:
        post_resp = client.post(
            "/api/v1/driver/target-events",
            headers=_bearer_header(configured_app),
            json=_target_body(),
        )
        event_id = post_resp.json()["id"]
        del_resp = client.delete(
            f"/api/v1/driver/target-events/{event_id}",
            headers=_bearer_header(configured_app),
        )
    assert del_resp.status_code == 204, del_resp.text
    rows = asyncio.run(_readiness_rows(factory, driver_id))
    assert len(rows) == 2, (
        f"expected 2 readiness snapshots (pin + unpin), got {len(rows)}"
    )
    assert rows[-1].target_event is None, (
        "latest snapshot after unpin must reflect no target"
    )
    assert rows[-1].event_pressure_input == 0.0


# -- track-day endpoint also recomputes --------------------------


def test_track_day_create_appends_readiness_snapshot(
    configured_app: dict[str, Any],
) -> None:
    """T088 POST /track-days creates a TargetEvent under the hood;
    the readiness hook must fire there too. Pin: a regression
    that wires the hook only on /target-events would leave the
    track-day path unable to update readiness."""
    factory = configured_app["factory"]
    driver_id = asyncio.run(
        _seed_driver(factory, clerk_id="caller")
    )
    body = {
        "venue_name": "Castle Combe Circuit",
        "starts_at": (
            datetime.now(UTC) + timedelta(days=21)
        ).isoformat(),
    }
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/driver/track-days",
            headers=_bearer_header(configured_app),
            json=body,
        )
    assert resp.status_code == 201, resp.text
    rows = asyncio.run(_readiness_rows(factory, driver_id))
    assert len(rows) == 1
    assert rows[0].target_event is not None
    assert (
        "Castle Combe Circuit" in rows[0].target_event["name"]
    )
    assert rows[0].target_event["kind"] == "real_track_day"
