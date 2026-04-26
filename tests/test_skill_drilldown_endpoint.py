"""T009 (sprint2-backend) — GET /api/v1/driver/skills/{dimension}.

Composite drilldown for one of the 10 SkillDimension fields on
DriverProgressSnapshot. Returns:
- ``current``  — the dimension's value on the most recent snapshot
                 (None if the driver has no snapshots yet)
- ``trend``    — ordered SkillTrendPoint history for charting
- ``best_session`` / ``worst_session`` — refs at the extremes of
                 the per-snapshot score series (None when <2 points)
- ``related_weaknesses`` — open WeaknessEpisode rows whose
                 ``category`` is the domain category mapped to the
                 requested dimension

Cross-driver isolation: 404 (not 403) for absent or not-owned
driver — same posture as `weaknesses.get_weakness_detail`.

Unknown dimension → 422 from FastAPI/Pydantic Literal validation.
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

from alembic import command
from app.deps import get_db, get_jwks_client
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import (
    DriverProfile,
    DriverProgressSnapshot,
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
            "sub": clerk_user_id,
            "email": f"{clerk_user_id}@t",
            "exp": int(time.time()) + 3600,
        },
        keys["kid"],
    )


def _empty_dim(score: float = 50.0) -> dict[str, Any]:
    return {
        "score": score,
        "confidence": 0.7,
        "trend_14d": "flat",
        "last_evidence_session_id": None,
        "sample_count": 5,
    }


def _all_dims(braking_score: float = 50.0) -> dict[str, Any]:
    """Build a payload of all 10 dimensions; braking score overridable
    so tests can vary the value being charted."""
    return {
        "braking": _empty_dim(braking_score),
        "throttle_pickup": _empty_dim(),
        "minimum_speed": _empty_dim(),
        "line_consistency": _empty_dim(),
        "tire_management": _empty_dim(),
        "racecraft": _empty_dim(),
        "fatigue_consistency": _empty_dim(),
        "strategy_execution": _empty_dim(),
        "setup_sensitivity": _empty_dim(),
        "adaptation": _empty_dim(),
    }


async def _seed_driver(
    factory: Any,
    *,
    clerk_user_id: str,
) -> dict[str, Any]:
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    async with factory() as db:
        db.add(
            User(
                id=user_id,
                clerk_user_id=clerk_user_id,
                email=f"{clerk_user_id}@t",
            )
        )
        db.add(
            DriverProfile(id=driver_id, user_id=user_id, display_name="A")
        )
        await db.commit()
    return {"user_id": user_id, "driver_id": driver_id}


async def _seed_snapshot(
    factory: Any,
    *,
    driver_id: Any,
    captured_at: datetime,
    braking_score: float,
    last_evidence_session_id: Any | None = None,
    window: str = "session",
) -> Any:
    snap_id = uuid6.uuid7()
    payload = _all_dims(braking_score)
    if last_evidence_session_id is not None:
        payload["braking"]["last_evidence_session_id"] = str(
            last_evidence_session_id
        )
    async with factory() as db:
        db.add(
            DriverProgressSnapshot(
                id=snap_id,
                driver_id=driver_id,
                captured_at=captured_at,
                window=window,
                window_start=captured_at - timedelta(hours=1),
                window_end=captured_at,
                braking=payload["braking"],
                throttle_pickup=payload["throttle_pickup"],
                minimum_speed=payload["minimum_speed"],
                line_consistency=payload["line_consistency"],
                tire_management=payload["tire_management"],
                racecraft=payload["racecraft"],
                fatigue_consistency=payload["fatigue_consistency"],
                strategy_execution=payload["strategy_execution"],
                setup_sensitivity=payload["setup_sensitivity"],
                adaptation=payload["adaptation"],
                overall_score=braking_score,
                overall_confidence=0.7,
                generated_by="test",
            )
        )
        await db.commit()
    return snap_id


async def _seed_weakness(
    factory: Any,
    *,
    driver_id: Any,
    category: str,
    status_: str = "open",
) -> Any:
    wid = uuid6.uuid7()
    async with factory() as db:
        db.add(
            WeaknessEpisode(
                id=wid,
                driver_id=driver_id,
                category=category,
                subcategory="x",
                first_observed_at=datetime(2026, 4, 1, tzinfo=UTC),
                last_observed_at=datetime(2026, 4, 25, tzinfo=UTC),
                recurrence_count=1,
                severity="medium",
                confidence=0.7,
                status=status_,
                library_rule_id="rule.x.v1",
            )
        )
        await db.commit()
    return wid


def _get(client: TestClient, ca: dict[str, Any], who: str, dim: str) -> Any:
    return client.get(
        f"/api/v1/driver/skills/{dim}",
        headers={"Authorization": f"Bearer {_bearer(ca['keys'], who)}"},
    )


# -- behavior ------------------------------------------------------


def test_drilldown_returns_current_dimension(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_dr")
    )
    asyncio.run(
        _seed_snapshot(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            captured_at=datetime(2026, 4, 25, 12, tzinfo=UTC),
            braking_score=70.0,
        )
    )
    client = TestClient(app)
    resp = _get(client, configured_app, "u_dr", "braking")
    assert resp.status_code == 200
    body = resp.json()
    assert body["dimension"] == "braking"
    assert body["current"] is not None
    assert body["current"]["score"] == 70.0


def test_drilldown_returns_empty_for_driver_without_snapshots(
    configured_app: dict[str, Any],
) -> None:
    asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_fresh")
    )
    client = TestClient(app)
    resp = _get(client, configured_app, "u_fresh", "braking")
    assert resp.status_code == 200
    body = resp.json()
    assert body["dimension"] == "braking"
    assert body["current"] is None
    assert body["trend"] == []
    assert body["best_session"] is None
    assert body["worst_session"] is None
    assert body["related_weaknesses"] == []


def test_drilldown_trend_ordered_oldest_to_newest(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_tr")
    )
    # seed three snapshots out of chronological order
    for hour, score in ((10, 50.0), (12, 70.0), (11, 60.0)):
        asyncio.run(
            _seed_snapshot(
                configured_app["factory"],
                driver_id=seeded["driver_id"],
                captured_at=datetime(2026, 4, 25, hour, tzinfo=UTC),
                braking_score=score,
            )
        )
    client = TestClient(app)
    resp = _get(client, configured_app, "u_tr", "braking")
    assert resp.status_code == 200
    body = resp.json()
    scores = [p["score"] for p in body["trend"]]
    assert scores == [50.0, 60.0, 70.0]


def test_drilldown_best_and_worst_session(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_bw")
    )
    best_sess = uuid6.uuid7()
    worst_sess = uuid6.uuid7()
    asyncio.run(
        _seed_snapshot(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            captured_at=datetime(2026, 4, 25, 10, tzinfo=UTC),
            braking_score=30.0,
            last_evidence_session_id=worst_sess,
        )
    )
    asyncio.run(
        _seed_snapshot(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            captured_at=datetime(2026, 4, 25, 12, tzinfo=UTC),
            braking_score=90.0,
            last_evidence_session_id=best_sess,
        )
    )
    client = TestClient(app)
    resp = _get(client, configured_app, "u_bw", "braking")
    assert resp.status_code == 200
    body = resp.json()
    assert body["best_session"] is not None
    assert body["worst_session"] is not None
    assert body["best_session"]["score"] == 90.0
    assert body["worst_session"]["score"] == 30.0
    assert body["best_session"]["session_id"] == str(best_sess)
    assert body["worst_session"]["session_id"] == str(worst_sess)


def test_drilldown_skips_best_worst_when_only_one_snapshot(
    configured_app: dict[str, Any],
) -> None:
    """With <2 snapshots there's no meaningful 'extremes' — return
    None for both rather than degenerate same-session refs."""
    seeded = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_one")
    )
    asyncio.run(
        _seed_snapshot(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            captured_at=datetime(2026, 4, 25, 10, tzinfo=UTC),
            braking_score=70.0,
            last_evidence_session_id=uuid6.uuid7(),
        )
    )
    client = TestClient(app)
    resp = _get(client, configured_app, "u_one", "braking")
    assert resp.status_code == 200
    body = resp.json()
    assert body["best_session"] is None
    assert body["worst_session"] is None


def test_drilldown_related_weaknesses_filtered_by_category(
    configured_app: dict[str, Any],
) -> None:
    """braking → 'technique' category. An open 'technique' weakness
    is included; an open 'race_management' weakness is not."""
    seeded = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_we")
    )
    asyncio.run(
        _seed_snapshot(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            captured_at=datetime(2026, 4, 25, 10, tzinfo=UTC),
            braking_score=50.0,
        )
    )
    asyncio.run(
        _seed_weakness(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            category="technique",
        )
    )
    asyncio.run(
        _seed_weakness(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            category="race_management",
        )
    )
    client = TestClient(app)
    resp = _get(client, configured_app, "u_we", "braking")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["related_weaknesses"]) == 1
    assert body["related_weaknesses"][0]["category"] == "technique"


def test_drilldown_excludes_resolved_weaknesses(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_re")
    )
    asyncio.run(
        _seed_snapshot(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            captured_at=datetime(2026, 4, 25, 10, tzinfo=UTC),
            braking_score=50.0,
        )
    )
    asyncio.run(
        _seed_weakness(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            category="technique",
            status_="resolved",
        )
    )
    client = TestClient(app)
    resp = _get(client, configured_app, "u_re", "braking")
    assert resp.status_code == 200
    assert resp.json()["related_weaknesses"] == []


def test_drilldown_unknown_dimension_returns_422(
    configured_app: dict[str, Any],
) -> None:
    asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_ud")
    )
    client = TestClient(app)
    resp = _get(client, configured_app, "u_ud", "not_a_real_dimension")
    assert resp.status_code == 422


def test_drilldown_for_user_without_profile_returns_404(
    configured_app: dict[str, Any],
) -> None:
    """User exists (created lazily on first authed request) but
    has no DriverProfile yet — 404."""
    client = TestClient(app)
    resp = _get(client, configured_app, "u_no_prof", "braking")
    assert resp.status_code == 404


def test_drilldown_does_not_leak_other_drivers_snapshots(
    configured_app: dict[str, Any],
) -> None:
    other = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_other")
    )
    asyncio.run(
        _seed_snapshot(
            configured_app["factory"],
            driver_id=other["driver_id"],
            captured_at=datetime(2026, 4, 25, 10, tzinfo=UTC),
            braking_score=80.0,
        )
    )
    asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_me")
    )
    client = TestClient(app)
    resp = _get(client, configured_app, "u_me", "braking")
    assert resp.status_code == 200
    body = resp.json()
    # u_me has a profile but no snapshots → empty drilldown
    assert body["current"] is None
    assert body["trend"] == []


@pytest.mark.usefixtures("configured_app")
def test_drilldown_unauthenticated_returns_401() -> None:
    client = TestClient(app)
    resp = client.get("/api/v1/driver/skills/braking")
    assert resp.status_code == 401
