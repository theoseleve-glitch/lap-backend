"""T083 (sprint2-backend) — DELETE /api/v1/driver/target-events/{id}.

Unpin a previously-pinned target. Returns 204 (no body) on success.

Authz pin: ownership scoping is the load-bearing guarantee here.
The DELETE filter MUST include `driver_id == caller.profile.id`
so one driver can't unpin another driver's target by guessing
or enumerating UUIDs. Returning 404 (not 403) for "exists but
not yours" is deliberate — 403 would let an attacker probe for
which UUIDs are valid pins. Same status surface as "no such id".

Acceptance:
  - 401 unauthenticated
  - 404 unknown id (no row matches)
  - 404 someone else's pin (row exists, different driver) AND
    the row is NOT deleted (regression check on the WHERE clause)
  - 204 success: returns no body, row is gone, OTHER drivers'
    pins are untouched
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


async def _seed_pin(
    factory: Any, *, driver_id: uuid6.UUID
) -> uuid6.UUID:
    async with factory() as db:
        event_id = uuid6.uuid7()
        db.add(
            TargetEvent(
                id=event_id,
                driver_id=driver_id,
                name="Test pin",
                kind="sim_event",
                starts_at=datetime(2026, 12, 12, 18, 0, tzinfo=UTC),
                competitive_weight=0.5,
            )
        )
        await db.commit()
        return event_id


async def _row_count(factory: Any) -> int:
    async with factory() as db:
        rows = (await db.execute(select(TargetEvent))).scalars().all()
        return len(rows)


def _delete(
    client: TestClient, ca: dict[str, Any], event_id: str,
    *, clerk_id: str = "caller",
) -> Any:
    return client.delete(
        f"/api/v1/driver/target-events/{event_id}",
        headers={"Authorization": f"Bearer {_bearer(ca['keys'], clerk_id)}"},
    )


# -- auth ---------------------------------------------------------


def test_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    """No bearer → 401. Pin: unpinning is auth-gated."""
    with TestClient(app) as client:
        resp = client.delete(
            f"/api/v1/driver/target-events/{uuid6.uuid7()}"
        )
    assert resp.status_code == 401


# -- not found ----------------------------------------------------


def test_unknown_event_id_returns_404(
    configured_app: dict[str, Any],
) -> None:
    """Authenticated caller, no such id → 404. Pin: returns 404
    (not 204) so the client can distinguish "already gone" from
    "successfully deleted just now"."""
    asyncio.run(_seed_driver(configured_app["factory"], clerk_id="caller"))
    with TestClient(app) as client:
        resp = _delete(client, configured_app, str(uuid6.uuid7()))
    assert resp.status_code == 404


# -- ownership scoping (the authz pin) ----------------------------


def test_someone_elses_pin_returns_404_and_does_not_delete(
    configured_app: dict[str, Any],
) -> None:
    """Caller A asks to delete a pin owned by caller B. The DELETE
    must:
      (1) return 404 — same status as "no such id" so an attacker
          can't enumerate other drivers' pin UUIDs.
      (2) leave B's row intact — the WHERE clause must filter by
          driver_id, not just by id.

    This is THE authz pin for the DELETE. A regression that drops
    the driver_id predicate would silently let any authenticated
    user delete any driver's pin if they can guess the UUID.
    """
    factory = configured_app["factory"]
    asyncio.run(_seed_driver(factory, clerk_id="caller"))
    other_driver_id = asyncio.run(
        _seed_driver(factory, clerk_id="victim")
    )
    pin_id = asyncio.run(
        _seed_pin(factory, driver_id=other_driver_id)
    )
    with TestClient(app) as client:
        resp = _delete(client, configured_app, str(pin_id))
    assert resp.status_code == 404
    # Crucially: the victim's row must still exist.
    assert asyncio.run(_row_count(factory)) == 1


# -- happy path ---------------------------------------------------


def test_owner_delete_returns_204_and_removes_row(
    configured_app: dict[str, Any],
) -> None:
    """Caller deletes their own pin → 204 with no body, row gone.
    Pin: the row must actually be removed from the DB, not just
    soft-deleted (T081 has no deleted_at column; if soft-delete
    becomes a requirement later, this test will surface it)."""
    factory = configured_app["factory"]
    driver_id = asyncio.run(_seed_driver(factory, clerk_id="caller"))
    pin_id = asyncio.run(_seed_pin(factory, driver_id=driver_id))
    with TestClient(app) as client:
        resp = _delete(client, configured_app, str(pin_id))
    assert resp.status_code == 204
    assert resp.content == b""
    assert asyncio.run(_row_count(factory)) == 0


def test_owner_delete_does_not_touch_other_drivers_pins(
    configured_app: dict[str, Any],
) -> None:
    """Two drivers each have a pin. Caller deletes their own.
    The other driver's pin survives. Pin: the driver_id predicate
    must scope the DELETE, not just identify which row to start
    with — a regression to a broader DELETE would wipe both."""
    factory = configured_app["factory"]
    caller_id = asyncio.run(_seed_driver(factory, clerk_id="caller"))
    other_id = asyncio.run(_seed_driver(factory, clerk_id="other"))
    caller_pin = asyncio.run(_seed_pin(factory, driver_id=caller_id))
    asyncio.run(_seed_pin(factory, driver_id=other_id))
    with TestClient(app) as client:
        resp = _delete(client, configured_app, str(caller_pin))
    assert resp.status_code == 204
    assert asyncio.run(_row_count(factory)) == 1


# -- driver not found --------------------------------------------


def test_user_without_driver_profile_returns_404(
    configured_app: dict[str, Any],
) -> None:
    """Authenticated user but no DriverProfile → 404. Pin: we
    surface missing-profile as 404 explicitly (not 500 from the
    None.id dereference)."""
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
        resp = _delete(client, configured_app, str(uuid6.uuid7()))
    assert resp.status_code == 404
    assert resp.json().get("detail") == "driver_not_found"
