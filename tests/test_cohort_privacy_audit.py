"""T061 (sprint2-backend) — cohort privacy audit.

ADR-008: "no user can query 'who scored higher than me'. Only
their own percentile." This file proves that property holds
through two complementary checks:

1. **Structural**: no benchmark/cohort endpoint accepts a
   `driver_id` query, path, or header parameter. The OpenAPI
   spec is the source of truth for the API surface; if a
   future regression exposes "fetch driver X's percentile",
   the spec audit catches it.

2. **Behavioral**: with two distinct authenticated users
   (alice, bob), each user's /benchmark response reflects
   THEIR snapshot values, never the other user's. Even if
   one user knew the other's UUID, they have no way to
   surface it through the API.

Why both: structural alone wouldn't catch a route that DOES
take driver_id implicitly (e.g. via a session cookie). The
behavioral test pins the runtime guarantee.
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


# -- structural: API surface audit --------------------------------


def test_no_benchmark_endpoint_accepts_driver_id_parameter() -> None:
    """OpenAPI audit: scan every operation under /api/v1/driver/
    that touches benchmark/cohort and assert NONE of them
    declares a `driver_id` parameter. If a future PR adds
    /api/v1/driver/{driver_id}/benchmark or similar, this trips.
    """
    schema = app.openapi()
    paths = schema.get("paths", {})
    suspicious_paths = []
    for path, ops in paths.items():
        if not ("benchmark" in path or "cohort" in path):
            continue
        for method, op in ops.items():
            if method not in ("get", "post", "patch", "delete", "put"):
                continue
            params = op.get("parameters", []) or []
            param_names = {p.get("name") for p in params}
            # The path itself should not contain {driver_id}.
            if "{driver_id}" in path:
                suspicious_paths.append(f"{method.upper()} {path}")
                continue
            if "driver_id" in param_names:
                suspicious_paths.append(
                    f"{method.upper()} {path} (param)"
                )
    assert suspicious_paths == [], (
        f"Privacy violation: benchmark/cohort endpoints expose "
        f"driver_id: {suspicious_paths}"
    )


# -- behavioral: cross-user isolation -----------------------------


async def _seed_two_drivers_with_distinct_scores(
    factory: Any,
) -> dict[str, Any]:
    """alice braking=10, bob braking=90. Same (track, car).
    Cohort distribution will distinguish them — alice ends up
    near the bottom, bob near the top."""
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
            name="911", car_class="gt3", master_car_id=master_car,
        )
        db.add_all([track, car])
        await db.commit()

        for clerk, brake_score in (("alice", 10.0), ("bob", 90.0)):
            user = User(
                id=uuid6.uuid7(), clerk_user_id=clerk,
                email=f"{clerk}@t",
            )
            driver = DriverProfile(
                id=uuid6.uuid7(), user_id=user.id,
                display_name=clerk, cohort_opt_in=True,
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
            snap = DriverProgressSnapshot(
                id=uuid6.uuid7(), driver_id=driver.id,
                captured_at=datetime(2026, 4, 26, tzinfo=UTC),
                window="session",
                window_start=datetime(2026, 4, 26, tzinfo=UTC),
                window_end=datetime(2026, 4, 26, tzinfo=UTC),
                braking=_dim(brake_score),
                throttle_pickup=_dim(60.0), minimum_speed=_dim(60.0),
                line_consistency=_dim(60.0), tire_management=_dim(60.0),
                racecraft=_dim(60.0), fatigue_consistency=_dim(60.0),
                strategy_execution=_dim(60.0),
                setup_sensitivity=_dim(60.0), adaptation=_dim(60.0),
                overall_score=60.0, overall_confidence=0.7,
                generated_by="t061-test",
            )
            db.add_all([user, driver, session, snap])

        # Pad cohort to >=20 distinct opt-in members so the
        # eligibility gate opens. Each padding member has
        # braking score 50 (middle of distribution).
        for i in range(18):
            user = User(
                id=uuid6.uuid7(), clerk_user_id=f"pad{i}",
                email=f"pad{i}@t",
            )
            driver = DriverProfile(
                id=uuid6.uuid7(), user_id=user.id,
                display_name=f"P{i}", cohort_opt_in=True,
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

        # Cohort metrics for braking: alice=10, bob=90, 18 mid=50
        for v in [10.0, 90.0] + [50.0] * 18:
            db.add(
                CohortMetric(
                    id=uuid6.uuid7(),
                    car_id=master_car, track_id=master_track,
                    metric="braking", value=v,
                    session_time=datetime(2026, 4, 26, tzinfo=UTC),
                )
            )
        await db.commit()
        return {
            "master_track": master_track, "master_car": master_car,
        }


def test_alice_query_does_not_leak_bobs_percentile(
    configured_app: dict[str, Any],
) -> None:
    """The strongest privacy pin: alice (braking=10) and bob
    (braking=90) coexist in the same cohort. Alice's /benchmark
    response reflects HER value (rank near 0.0 — she's at the
    bottom on braking). Bob's response would reflect his value
    (~1.0 — he's at the top). Alice has no way to see bob's
    rank — her response is purely derived from her own snapshot.
    """
    factory = configured_app["factory"]
    ids = asyncio.run(_seed_two_drivers_with_distinct_scores(factory))
    with TestClient(app) as client:
        alice_resp = client.get(
            "/api/v1/driver/benchmark",
            params={
                "car_id": str(ids["master_car"]),
                "track_id": str(ids["master_track"]),
            },
            headers={
                "Authorization": (
                    f"Bearer {_bearer(configured_app['keys'], 'alice')}"
                ),
            },
        )
        bob_resp = client.get(
            "/api/v1/driver/benchmark",
            params={
                "car_id": str(ids["master_car"]),
                "track_id": str(ids["master_track"]),
            },
            headers={
                "Authorization": (
                    f"Bearer {_bearer(configured_app['keys'], 'bob')}"
                ),
            },
        )
    alice_rank = alice_resp.json()["percentiles"]["braking"]
    bob_rank = bob_resp.json()["percentiles"]["braking"]
    # alice value=10 → rank should be near 0 (everyone above her)
    # bob value=90 → rank should be near 1 (everyone below)
    # The hard pin is that they are DIFFERENT — alice cannot
    # surface bob's rank.
    assert alice_rank < 0.1, (
        f"alice's rank should be near 0, got {alice_rank}"
    )
    assert bob_rank > 0.9, (
        f"bob's rank should be near 1, got {bob_rank}"
    )
    assert alice_rank != bob_rank
