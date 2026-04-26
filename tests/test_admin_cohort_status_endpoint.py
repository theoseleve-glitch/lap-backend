"""T064 (sprint2-backend) — GET /api/v1/admin/cohort/status.

Operator-facing endpoint that lists every (master_car,
master_track) cohort with current size and unlock status.
Used to spot cohorts that are nearly unlocked, completely
empty, or where ingestion has stalled.

Auth-gated by require_admin (User.is_admin). Non-admin users
get 403; non-authenticated users get 401.

Acceptance:
  - non-admin authenticated → 403
  - admin → 200 with one entry per distinct cohort
  - each entry has car_id, track_id, cohort_size, unlocked
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
    CarModel,
    CohortMetric,
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


async def _seed_user(
    factory: Any, *, clerk_id: str, is_admin: bool,
) -> None:
    async with factory() as db:
        db.add(
            User(
                id=uuid6.uuid7(), clerk_user_id=clerk_id,
                email=f"{clerk_id}@t", is_admin=is_admin,
            )
        )
        await db.commit()


async def _seed_cohort_with_n_drivers(
    factory: Any, *, label: str, n: int,
) -> dict[str, Any]:
    """Seed a (track, car) plus n opt-in drivers each with one
    session and a cohort_metric row. Returns master_track and
    master_car so the test can assert on them."""
    master_track = uuid6.uuid7()
    master_car = uuid6.uuid7()
    async with factory() as db:
        track = TrackLayout(
            id=uuid6.uuid7(), source_provider=f"iracing-{label}",
            name=f"Spa-{label}", layout_name="GP",
            start_finish_lat=50.4, start_finish_lon=5.97,
            start_finish_heading_deg=10.0,
            sector_checkpoints=[], corner_names=[],
            master_track_id=master_track,
        )
        car = CarModel(
            id=uuid6.uuid7(), source_provider=f"iracing-{label}",
            name=f"911-{label}", car_class="gt3",
            master_car_id=master_car,
        )
        db.add_all([track, car])
        await db.commit()
        for i in range(n):
            user = User(
                id=uuid6.uuid7(), clerk_user_id=f"{label}u{i}",
                email=f"{label}u{i}@t",
            )
            driver = DriverProfile(
                id=uuid6.uuid7(), user_id=user.id,
                display_name=f"{label}D{i}", cohort_opt_in=True,
            )
            session = SessionRow(
                id=uuid6.uuid7(), driver_id=driver.id,
                manifest={
                    "track_id": str(track.id),
                    "car_id": str(car.id),
                },
                created_at=datetime(2026, 4, 26, tzinfo=UTC),
                status="processed",
            )
            db.add_all([user, driver, session])
            db.add(
                CohortMetric(
                    id=uuid6.uuid7(),
                    car_id=master_car, track_id=master_track,
                    metric="braking", value=50.0,
                    session_time=datetime(2026, 4, 26, tzinfo=UTC),
                )
            )
        await db.commit()
        return {
            "master_track": master_track, "master_car": master_car,
        }


def _get_status(
    client: TestClient, ca: dict[str, Any], who: str,
) -> Any:
    return client.get(
        "/api/v1/admin/cohort/status",
        headers={"Authorization": f"Bearer {_bearer(ca['keys'], who)}"},
    )


# -- auth ---------------------------------------------------------


def test_non_admin_returns_403(configured_app: dict[str, Any]) -> None:
    """Auth user without is_admin → 403. Pin the require_admin
    dep so the endpoint never accidentally exposes operator data
    to regular users."""
    asyncio.run(
        _seed_user(
            configured_app["factory"], clerk_id="alice", is_admin=False,
        )
    )
    with TestClient(app) as client:
        resp = _get_status(client, configured_app, "alice")
    assert resp.status_code == 403


# -- happy paths --------------------------------------------------


def test_admin_with_no_cohorts_gets_empty_list(
    configured_app: dict[str, Any],
) -> None:
    """Empty cohort_metrics → 200 with empty cohorts list. Pin
    the no-fallback-on-empty posture (the operator sees "no
    cohorts yet", not an error)."""
    asyncio.run(
        _seed_user(
            configured_app["factory"], clerk_id="root", is_admin=True,
        )
    )
    with TestClient(app) as client:
        resp = _get_status(client, configured_app, "root")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cohorts"] == []


def test_admin_sees_all_distinct_cohorts(
    configured_app: dict[str, Any],
) -> None:
    """Two distinct (car, track) cohorts → two entries in the
    response, one per cohort. Pins the GROUP BY shape."""
    factory = configured_app["factory"]
    asyncio.run(_seed_user(factory, clerk_id="root", is_admin=True))
    a = asyncio.run(
        _seed_cohort_with_n_drivers(factory, label="alpha", n=3)
    )
    b = asyncio.run(
        _seed_cohort_with_n_drivers(factory, label="beta", n=22)
    )
    with TestClient(app) as client:
        resp = _get_status(client, configured_app, "root")
    body = resp.json()
    assert len(body["cohorts"]) == 2
    by_car = {c["car_id"]: c for c in body["cohorts"]}
    assert str(a["master_car"]) in by_car
    assert str(b["master_car"]) in by_car


def test_each_entry_has_size_and_unlocked_fields(
    configured_app: dict[str, Any],
) -> None:
    """Each entry includes cohort_size and unlocked. The unlocked
    flag derives from the T056 threshold check — alpha (3
    drivers) is locked, beta (22) is unlocked."""
    factory = configured_app["factory"]
    asyncio.run(_seed_user(factory, clerk_id="root", is_admin=True))
    a = asyncio.run(
        _seed_cohort_with_n_drivers(factory, label="alpha", n=3)
    )
    b = asyncio.run(
        _seed_cohort_with_n_drivers(factory, label="beta", n=22)
    )
    with TestClient(app) as client:
        resp = _get_status(client, configured_app, "root")
    body = resp.json()
    by_car = {c["car_id"]: c for c in body["cohorts"]}
    alpha = by_car[str(a["master_car"])]
    beta = by_car[str(b["master_car"])]
    assert alpha["cohort_size"] == 3
    assert alpha["unlocked"] is False
    assert beta["cohort_size"] == 22
    assert beta["unlocked"] is True
