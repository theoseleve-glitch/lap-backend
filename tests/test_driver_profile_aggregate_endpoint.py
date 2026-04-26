"""T004 (sprint2-backend) — GET /api/v1/driver/profile aggregate.

One round-trip surfaces:
- latest progress snapshot
- top-5 weakness episodes (severity DESC then last_observed_at DESC)
- active prescriptions (status in proposed/accepted/in_progress)
- latest readiness state

The <200ms p95 budget is asserted by the k6 load test in
`scripts/k6/driver_profile.js` (T231 wired this), not here.
These tests cover correctness; load tests cover latency.
"""

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
from sqlalchemy.ext.asyncio import AsyncSession

from alembic import command
from app.deps import get_db, get_jwks_client
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import (
    ActionPrescription,
    DriverProfile,
    DriverProgressSnapshot,
    DriverReadinessState,
    User,
    WeaknessEpisode,
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
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": _b64u(n),
        "e": _b64u(e),
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

    async def fake_get_db() -> AsyncGenerator[AsyncSession, None]:
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


# -- seed helpers --------------------------------------------------


async def _seed_driver(
    factory: Any, *, clerk_user_id: str, email: str
) -> dict[str, Any]:
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    async with factory() as db:
        db.add(User(id=user_id, clerk_user_id=clerk_user_id, email=email))
        db.add(DriverProfile(id=driver_id, user_id=user_id, display_name="A"))
        await db.commit()
    return {"user_id": user_id, "driver_id": driver_id}


def _empty_skill() -> dict[str, Any]:
    return {
        "score": 70.0,
        "confidence": 0.7,
        "trend_14d": "flat",
        "sample_count": 5,
    }


async def _seed_progress(
    factory: Any, *, driver_id: Any, captured_at: datetime, score: float = 70.0
) -> Any:
    snap_id = uuid6.uuid7()
    async with factory() as db:
        db.add(
            DriverProgressSnapshot(
                id=snap_id,
                driver_id=driver_id,
                captured_at=captured_at,
                window="rolling_30d",
                window_start=captured_at - timedelta(days=30),
                window_end=captured_at,
                braking=_empty_skill(),
                throttle_pickup=_empty_skill(),
                minimum_speed=_empty_skill(),
                line_consistency=_empty_skill(),
                tire_management=_empty_skill(),
                racecraft=_empty_skill(),
                fatigue_consistency=_empty_skill(),
                strategy_execution=_empty_skill(),
                setup_sensitivity=_empty_skill(),
                adaptation=_empty_skill(),
                overall_score=score,
                overall_confidence=0.85,
                generated_by="rules-v1",
            )
        )
        await db.commit()
    return snap_id


async def _seed_weakness(
    factory: Any,
    *,
    driver_id: Any,
    severity: str,
    last_observed_at: datetime,
    library_rule_id: str = "rule.x.v1",
) -> Any:
    ep_id = uuid6.uuid7()
    async with factory() as db:
        db.add(
            WeaknessEpisode(
                id=ep_id,
                driver_id=driver_id,
                category="technique",
                subcategory="late_release",
                first_observed_at=last_observed_at - timedelta(days=7),
                last_observed_at=last_observed_at,
                recurrence_count=3,
                severity=severity,
                confidence=0.7,
                status="open",
                library_rule_id=library_rule_id,
            )
        )
        await db.commit()
    return ep_id


async def _seed_prescription(
    factory: Any, *, driver_id: Any, status: str, title: str = "drill X"
) -> Any:
    pid = uuid6.uuid7()
    async with factory() as db:
        db.add(
            ActionPrescription(
                id=pid,
                driver_id=driver_id,
                weakness_episode_id=None,
                created_at=datetime(2026, 4, 26, tzinfo=UTC),
                priority="p0",
                title=title,
                rationale="...",
                drill_template_id="drill.x.v1",
                expected_context={},
                success_condition={"comparison": "gte", "target_pct": 0.85},
                status=status,
            )
        )
        await db.commit()
    return pid


async def _seed_readiness(
    factory: Any, *, driver_id: Any, computed_at: datetime, score: float = 70.0
) -> Any:
    rid = uuid6.uuid7()
    async with factory() as db:
        db.add(
            DriverReadinessState(
                id=rid,
                driver_id=driver_id,
                computed_at=computed_at,
                readiness_score=score,
                volume_input=0.7,
                quality_input=0.8,
                recency_input=0.9,
                consistency_input=0.6,
                unresolved_weaknesses_input=0.5,
                familiarity_input=0.7,
                event_pressure_input=0.4,
                recovery_input=0.8,
                limiting_factor="line_consistency",
                likely_gain_area="braking",
                recommended_sessions_before_event=3,
            )
        )
        await db.commit()
    return rid


def _bearer(keys: dict[str, Any], clerk_user_id: str) -> str:
    return _sign(
        keys["priv_pem"],
        {
            "sub": clerk_user_id,
            "email": f"{clerk_user_id}@t",
            "exp": int(time.time()) + 3600,
        },
        keys["kid"],
    )


def _hit(client: TestClient, configured_app: dict[str, Any], who: str) -> Any:
    return client.get(
        "/api/v1/driver/profile",
        headers={"Authorization": f"Bearer {_bearer(configured_app['keys'], who)}"},
    )


# -- behavior ------------------------------------------------------


@pytest.mark.usefixtures("configured_app")
def test_unauthenticated_returns_401() -> None:
    client = TestClient(app)
    resp = client.get("/api/v1/driver/profile")
    assert resp.status_code == 401


def test_empty_driver_returns_well_shaped_nulls(
    configured_app: dict[str, Any],
) -> None:
    asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_empty", email="e@t")
    )
    client = TestClient(app)
    body = _hit(client, configured_app, "u_empty").json()
    assert body["progress"] is None
    assert body["readiness"] is None
    assert body["top_weaknesses"] == []
    assert body["active_prescriptions"] == []


def test_progress_section_returns_latest_snapshot(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_p", email="p@t")
    )
    asyncio.run(
        _seed_progress(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            captured_at=datetime(2026, 4, 20, tzinfo=UTC),
            score=60.0,  # older
        )
    )
    asyncio.run(
        _seed_progress(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            captured_at=datetime(2026, 4, 25, tzinfo=UTC),
            score=82.0,  # newer
        )
    )
    client = TestClient(app)
    body = _hit(client, configured_app, "u_p").json()
    assert body["progress"]["overall_score"] == 82.0


def test_top_weaknesses_capped_at_five_severity_first(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_w", email="w@t")
    )
    base = datetime(2026, 4, 26, tzinfo=UTC)
    # 3 high-severity + 4 medium-severity + 1 low — top-5 must
    # surface the 3 highs and 2 mediums (severity tiebreaker
    # falls back to last_observed_at desc within tier).
    for i in range(3):
        asyncio.run(
            _seed_weakness(
                configured_app["factory"],
                driver_id=seeded["driver_id"],
                severity="high",
                last_observed_at=base - timedelta(hours=i),
                library_rule_id=f"high_{i}",
            )
        )
    for i in range(4):
        asyncio.run(
            _seed_weakness(
                configured_app["factory"],
                driver_id=seeded["driver_id"],
                severity="medium",
                last_observed_at=base - timedelta(days=i),
                library_rule_id=f"med_{i}",
            )
        )
    asyncio.run(
        _seed_weakness(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            severity="low",
            last_observed_at=base,
            library_rule_id="low_0",
        )
    )
    client = TestClient(app)
    body = _hit(client, configured_app, "u_w").json()
    assert len(body["top_weaknesses"]) == 5
    severities = [w["severity"] for w in body["top_weaknesses"]]
    assert severities[:3] == ["high", "high", "high"]
    assert severities[3:] == ["medium", "medium"]


def test_active_prescriptions_filters_out_terminal_statuses(
    configured_app: dict[str, Any],
) -> None:
    """Only proposed/accepted/in_progress should appear; completed,
    skipped, expired must NOT — they're done, not active."""
    seeded = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_rx", email="r@t")
    )
    for status in ("proposed", "accepted", "in_progress"):
        asyncio.run(
            _seed_prescription(
                configured_app["factory"],
                driver_id=seeded["driver_id"],
                status=status,
                title=f"open_{status}",
            )
        )
    for status in ("completed", "skipped", "expired"):
        asyncio.run(
            _seed_prescription(
                configured_app["factory"],
                driver_id=seeded["driver_id"],
                status=status,
                title=f"done_{status}",
            )
        )
    client = TestClient(app)
    body = _hit(client, configured_app, "u_rx").json()
    titles = {p["title"] for p in body["active_prescriptions"]}
    assert titles == {"open_proposed", "open_accepted", "open_in_progress"}


def test_readiness_returns_latest_state(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_rd", email="r2@t")
    )
    asyncio.run(
        _seed_readiness(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            computed_at=datetime(2026, 4, 20, tzinfo=UTC),
            score=55.0,
        )
    )
    asyncio.run(
        _seed_readiness(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            computed_at=datetime(2026, 4, 25, tzinfo=UTC),
            score=78.0,
        )
    )
    client = TestClient(app)
    body = _hit(client, configured_app, "u_rd").json()
    assert body["readiness"]["readiness_score"] == 78.0


def test_other_drivers_data_is_invisible(
    configured_app: dict[str, Any],
) -> None:
    """Driver A must not see driver B's progress/weaknesses/etc.
    Cross-driver isolation in the aggregate."""
    me = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_me", email="m@t")
    )
    other = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_o", email="o@t")
    )
    asyncio.run(
        _seed_progress(
            configured_app["factory"],
            driver_id=other["driver_id"],
            captured_at=datetime(2026, 4, 26, tzinfo=UTC),
            score=99.0,
        )
    )
    asyncio.run(
        _seed_weakness(
            configured_app["factory"],
            driver_id=other["driver_id"],
            severity="high",
            last_observed_at=datetime(2026, 4, 26, tzinfo=UTC),
        )
    )
    asyncio.run(
        _seed_prescription(
            configured_app["factory"],
            driver_id=other["driver_id"],
            status="proposed",
            title="other_driver_drill",
        )
    )
    client = TestClient(app)
    body = _hit(client, configured_app, "u_me").json()
    assert body["progress"] is None
    assert body["top_weaknesses"] == []
    assert body["active_prescriptions"] == []
    # Bind reference so linter doesn't drop the seeded variables
    assert me["driver_id"] != other["driver_id"]
