"""T058 (sprint2-backend) — GET /api/v1/driver/benchmark.

The driver hits this endpoint with (car_id, track_id) — both
master IDs — and gets back their percentile rank per dimension
in that cohort. If the cohort hasn't unlocked yet (per T056 /
ADR-008: <20 distinct opted-in drivers), `percentiles` is null.

Acceptance:
  - cohort eligible AND driver has snapshot → percentiles dict
    with one entry per dimension that has cohort data
  - cohort NOT eligible → percentiles is null
  - no driver profile → 404
  - missing query params → 422
  - cross-driver isolation: response only ever uses the auth'd
    user's snapshot values (never another driver's)
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
    DriverProgressSnapshot,
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


def _dim(score: float) -> dict[str, Any]:
    return {
        "score": score, "confidence": 0.8, "trend_14d": "flat",
        "last_evidence_session_id": None, "sample_count": 5,
    }


async def _seed_one_driver_with_snapshot(
    factory: Any, *, clerk_id: str, opt_in: bool,
    track_id: Any, car_id: Any, score_braking: float = 50.0,
) -> dict[str, Any]:
    async with factory() as db:
        user = User(
            id=uuid6.uuid7(), clerk_user_id=clerk_id,
            email=f"{clerk_id}@t",
        )
        driver = DriverProfile(
            id=uuid6.uuid7(), user_id=user.id, display_name=clerk_id,
            cohort_opt_in=opt_in,
        )
        session = SessionRow(
            id=uuid6.uuid7(), driver_id=driver.id,
            manifest={"track_id": str(track_id), "car_id": str(car_id)},
            created_at=datetime(2026, 4, 26, 14, tzinfo=UTC),
            status="processed",
        )
        snap = DriverProgressSnapshot(
            id=uuid6.uuid7(), driver_id=driver.id,
            captured_at=datetime(2026, 4, 26, tzinfo=UTC),
            window="session",
            window_start=datetime(2026, 4, 26, tzinfo=UTC),
            window_end=datetime(2026, 4, 26, tzinfo=UTC),
            braking=_dim(score_braking),
            throttle_pickup=_dim(60.0), minimum_speed=_dim(60.0),
            line_consistency=_dim(60.0), tire_management=_dim(60.0),
            racecraft=_dim(60.0), fatigue_consistency=_dim(60.0),
            strategy_execution=_dim(60.0), setup_sensitivity=_dim(60.0),
            adaptation=_dim(60.0),
            overall_score=60.0, overall_confidence=0.7,
            generated_by="t058-test",
        )
        db.add_all([user, driver, session, snap])
        await db.commit()
        return {"driver_id": driver.id}


async def _seed_track_car(factory: Any, *, label: str = "a") -> dict[str, Any]:
    """Returns master_track, master_car, track_id, car_id (the
    catalog row IDs used by the session manifest)."""
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
        return {
            "master_track": master_track, "master_car": master_car,
            "track_id": track.id, "car_id": car.id,
        }


async def _seed_cohort(
    factory: Any, *, master_track: Any, master_car: Any,
    track_id: Any, car_id: Any, n_drivers: int,
    metric: str = "braking", values: list[float] | None = None,
) -> None:
    """Seed N opt-in drivers each with one session (to satisfy
    T056 cohort minimum) and N cohort_metric rows for `metric`."""
    if values is None:
        values = [float(x) for x in range(0, n_drivers * 5, 5)]
    async with factory() as db:
        for i, v in enumerate(values):
            user = User(
                id=uuid6.uuid7(), clerk_user_id=f"cohort{i}",
                email=f"cohort{i}@t",
            )
            driver = DriverProfile(
                id=uuid6.uuid7(), user_id=user.id,
                display_name=f"C{i}", cohort_opt_in=True,
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
            db.add(
                CohortMetric(
                    id=uuid6.uuid7(),
                    car_id=master_car, track_id=master_track,
                    metric=metric, value=v,
                    session_time=datetime(2026, 4, 26, tzinfo=UTC),
                )
            )
        await db.commit()


def _get_benchmark(
    client: TestClient, ca: dict[str, Any], who: str,
    *, car_id: Any, track_id: Any,
) -> Any:
    return client.get(
        "/api/v1/driver/benchmark",
        params={"car_id": str(car_id), "track_id": str(track_id)},
        headers={"Authorization": f"Bearer {_bearer(ca['keys'], who)}"},
    )


# -- happy paths --------------------------------------------------


def test_returns_null_percentiles_when_cohort_below_minimum(
    configured_app: dict[str, Any],
) -> None:
    """Cohort < 20 drivers → percentiles is null. Pin the
    cohort-locked posture (ADR-008)."""
    factory = configured_app["factory"]
    ids = asyncio.run(_seed_track_car(factory))
    asyncio.run(
        _seed_one_driver_with_snapshot(
            factory, clerk_id="alice", opt_in=True,
            track_id=ids["track_id"], car_id=ids["car_id"],
        )
    )
    asyncio.run(
        _seed_cohort(
            factory,
            master_track=ids["master_track"], master_car=ids["master_car"],
            track_id=ids["track_id"], car_id=ids["car_id"], n_drivers=5,
        )
    )
    with TestClient(app) as client:
        resp = _get_benchmark(
            client, configured_app, "alice",
            car_id=ids["master_car"], track_id=ids["master_track"],
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["percentiles"] is None


def test_returns_percentiles_dict_when_cohort_eligible(
    configured_app: dict[str, Any],
) -> None:
    """Cohort >= 20 → percentiles dict with the driver's rank
    in the braking distribution. Driver scored 50.0; cohort
    has values 0,5,10,...,95 (20 values). Midrank for 50:
    #strictly_below=10, #equal=1 (the row alice contributed
    via the seed function happens to be 50 if values include
    it; we check it's a sensible value in [0,1])."""
    factory = configured_app["factory"]
    ids = asyncio.run(_seed_track_car(factory))
    asyncio.run(
        _seed_one_driver_with_snapshot(
            factory, clerk_id="alice", opt_in=True,
            track_id=ids["track_id"], car_id=ids["car_id"],
            score_braking=50.0,
        )
    )
    asyncio.run(
        _seed_cohort(
            factory,
            master_track=ids["master_track"], master_car=ids["master_car"],
            track_id=ids["track_id"], car_id=ids["car_id"],
            n_drivers=20,
            values=[float(x) for x in range(0, 100, 5)],
        )
    )
    with TestClient(app) as client:
        resp = _get_benchmark(
            client, configured_app, "alice",
            car_id=ids["master_car"], track_id=ids["master_track"],
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["percentiles"] is not None
    assert "braking" in body["percentiles"]
    rank = body["percentiles"]["braking"]
    # 10 strictly below 50 + 0.5*1 equal = 10.5/20 = 0.525
    assert 0.5 <= rank <= 0.55


def test_response_echoes_master_car_and_track(
    configured_app: dict[str, Any],
) -> None:
    """Response includes the master car/track IDs from the request
    so the client can verify it got the right cohort back."""
    factory = configured_app["factory"]
    ids = asyncio.run(_seed_track_car(factory))
    asyncio.run(
        _seed_one_driver_with_snapshot(
            factory, clerk_id="alice", opt_in=True,
            track_id=ids["track_id"], car_id=ids["car_id"],
        )
    )
    with TestClient(app) as client:
        resp = _get_benchmark(
            client, configured_app, "alice",
            car_id=ids["master_car"], track_id=ids["master_track"],
        )
    body = resp.json()
    assert body["car_id"] == str(ids["master_car"])
    assert body["track_id"] == str(ids["master_track"])


# -- errors -------------------------------------------------------


def test_no_driver_profile_returns_404(
    configured_app: dict[str, Any],
) -> None:
    """Auth user has no DriverProfile row → 404. Mirror of T053
    bootstrap-flow boundary."""
    factory = configured_app["factory"]

    async def seed_user_only() -> None:
        async with factory() as db:
            db.add(
                User(
                    id=uuid6.uuid7(), clerk_user_id="alice",
                    email="a@t",
                )
            )
            await db.commit()

    asyncio.run(seed_user_only())
    with TestClient(app) as client:
        resp = _get_benchmark(
            client, configured_app, "alice",
            car_id=uuid6.uuid7(), track_id=uuid6.uuid7(),
        )
    assert resp.status_code == 404


def test_missing_query_params_returns_422(
    configured_app: dict[str, Any],
) -> None:
    """car_id and track_id are required query params."""
    factory = configured_app["factory"]
    asyncio.run(
        _seed_one_driver_with_snapshot(
            factory, clerk_id="alice", opt_in=True,
            track_id=uuid6.uuid7(), car_id=uuid6.uuid7(),
        )
    )
    with TestClient(app) as client:
        resp = client.get(
            "/api/v1/driver/benchmark",
            headers={
                "Authorization": (
                    f"Bearer {_bearer(configured_app['keys'], 'alice')}"
                ),
            },
        )
    assert resp.status_code == 422
