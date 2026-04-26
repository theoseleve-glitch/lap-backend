"""T059 (sprint2-backend) — GET /api/v1/driver/cohort_size.

Sister endpoint to T058 /benchmark. When the cohort hasn't
unlocked, /benchmark returns null percentiles; the UI then
hits this endpoint to render "17 / 20 drivers — unlocks at
20" progress.

Acceptance:
  - empty cohort → cohort_size=0
  - sub-threshold cohort (e.g. 17) → cohort_size=17
  - threshold-met cohort (20) → cohort_size=20
  - response includes the server-controlled unlock_threshold
  - 422 on missing query params
  - auth required (mirror of /benchmark)
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
from domain.cohort_minimum import MIN_COHORT_SIZE
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import (
    CarModel,
    DriverProfile,
    TrackLayout,
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
    return jwt.encode(claims, priv_pem, algorithm="RS256", headers={"kid": kid})


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
            "https://test.clerk", transport=httpx.MockTransport(handler)
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


async def _seed_track_car(factory: Any) -> dict[str, Any]:
    master_track = uuid6.uuid7()
    master_car = uuid6.uuid7()
    async with factory() as db:
        track = TrackLayout(
            id=uuid6.uuid7(), source_provider="iracing",
            name="Spa", layout_name="GP",
            start_finish_lat=50.4, start_finish_lon=5.97,
            start_finish_heading_deg=10.0,
            sector_checkpoints=[], corner_names=[],
            master_track_id=master_track,
        )
        car = CarModel(
            id=uuid6.uuid7(), source_provider="iracing",
            name="911", car_class="gt3",
            master_car_id=master_car,
        )
        db.add_all([track, car])
        await db.commit()
        return {
            "master_track": master_track, "master_car": master_car,
            "track_id": track.id, "car_id": car.id,
        }


async def _seed_caller(factory: Any) -> None:
    """Auth user must have a User row to authenticate. We don't
    require a DriverProfile — cohort_size isn't user-scoped, it's
    just authenticated."""
    async with factory() as db:
        db.add(
            User(
                id=uuid6.uuid7(), clerk_user_id="alice", email="a@t",
            )
        )
        await db.commit()


async def _seed_n_opt_in_drivers(
    factory: Any, *, track_id: Any, car_id: Any, n: int,
) -> None:
    async with factory() as db:
        for i in range(n):
            user = User(
                id=uuid6.uuid7(), clerk_user_id=f"d{i}",
                email=f"d{i}@t",
            )
            driver = DriverProfile(
                id=uuid6.uuid7(), user_id=user.id,
                display_name=f"D{i}", cohort_opt_in=True,
            )
            session = SessionRow(
                id=uuid6.uuid7(), driver_id=driver.id,
                manifest={
                    "track_id": str(track_id), "car_id": str(car_id),
                },
                created_at=datetime(2026, 4, 26, tzinfo=UTC),
                status="processed",
            )
            db.add_all([user, driver, session])
        await db.commit()


def _get_size(
    client: TestClient, ca: dict[str, Any], who: str,
    *, car_id: Any, track_id: Any,
) -> Any:
    return client.get(
        "/api/v1/driver/cohort_size",
        params={"car_id": str(car_id), "track_id": str(track_id)},
        headers={"Authorization": f"Bearer {_bearer(ca['keys'], who)}"},
    )


# -- counts -------------------------------------------------------


def test_returns_zero_for_empty_cohort(
    configured_app: dict[str, Any],
) -> None:
    """No drivers seeded → cohort_size=0. Pin "no fallback to
    unlocked" — the UI must show 0/20 not "unlocked"."""
    factory = configured_app["factory"]
    asyncio.run(_seed_caller(factory))
    asyncio.run(_seed_track_car(factory))
    with TestClient(app) as client:
        resp = _get_size(
            client, configured_app, "alice",
            car_id=uuid6.uuid7(), track_id=uuid6.uuid7(),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["cohort_size"] == 0


def test_returns_count_below_threshold(
    configured_app: dict[str, Any],
) -> None:
    """17 opt-in drivers → cohort_size=17 (below MIN_COHORT_SIZE).
    The UI uses this to render '17 / 20' progress."""
    factory = configured_app["factory"]
    asyncio.run(_seed_caller(factory))
    ids = asyncio.run(_seed_track_car(factory))
    asyncio.run(
        _seed_n_opt_in_drivers(
            factory, track_id=ids["track_id"], car_id=ids["car_id"], n=17,
        )
    )
    with TestClient(app) as client:
        resp = _get_size(
            client, configured_app, "alice",
            car_id=ids["master_car"], track_id=ids["master_track"],
        )
    body = resp.json()
    assert body["cohort_size"] == 17


def test_returns_count_at_threshold(
    configured_app: dict[str, Any],
) -> None:
    """Exactly MIN_COHORT_SIZE drivers → cohort_size matches.
    Different from /benchmark which gates eligibility — this
    endpoint reports the raw count regardless of unlock state."""
    factory = configured_app["factory"]
    asyncio.run(_seed_caller(factory))
    ids = asyncio.run(_seed_track_car(factory))
    asyncio.run(
        _seed_n_opt_in_drivers(
            factory, track_id=ids["track_id"], car_id=ids["car_id"],
            n=MIN_COHORT_SIZE,
        )
    )
    with TestClient(app) as client:
        resp = _get_size(
            client, configured_app, "alice",
            car_id=ids["master_car"], track_id=ids["master_track"],
        )
    body = resp.json()
    assert body["cohort_size"] == MIN_COHORT_SIZE


# -- threshold field ----------------------------------------------


def test_response_includes_unlock_threshold(
    configured_app: dict[str, Any],
) -> None:
    """The unlock threshold is server-controlled (not UI-hardcoded)
    — pin it in the response so a server-side change to the policy
    propagates to the UI without a client release."""
    factory = configured_app["factory"]
    asyncio.run(_seed_caller(factory))
    ids = asyncio.run(_seed_track_car(factory))
    with TestClient(app) as client:
        resp = _get_size(
            client, configured_app, "alice",
            car_id=ids["master_car"], track_id=ids["master_track"],
        )
    body = resp.json()
    assert body["unlock_threshold"] == MIN_COHORT_SIZE


# -- errors -------------------------------------------------------


def test_missing_query_params_returns_422(
    configured_app: dict[str, Any],
) -> None:
    """car_id and track_id are required."""
    factory = configured_app["factory"]
    asyncio.run(_seed_caller(factory))
    with TestClient(app) as client:
        resp = client.get(
            "/api/v1/driver/cohort_size",
            headers={
                "Authorization": (
                    f"Bearer {_bearer(configured_app['keys'], 'alice')}"
                ),
            },
        )
    assert resp.status_code == 422
