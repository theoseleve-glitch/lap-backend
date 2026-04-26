"""T007 (sprint2-backend) — GET /api/v1/driver/weaknesses/{id}.

Returns a WeaknessDetail with the episode + history (sessions
where observed) + evidence (linked CoachingInsights) +
linked_prescriptions.

Cross-driver isolation: 404 (not 403) for absent OR not-owned
weakness ids — same convention as compare_cases / sessions.
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
    CoachingInsight,
    DriverProfile,
    User,
    WeaknessEpisode,
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


async def _seed_driver(
    factory: Any, *, clerk_user_id: str
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
        db.add(DriverProfile(id=driver_id, user_id=user_id, display_name="A"))
        await db.commit()
    return {"user_id": user_id, "driver_id": driver_id}


async def _seed_session(
    factory: Any, *, driver_id: Any, created_at: datetime
) -> Any:
    sid = uuid6.uuid7()
    async with factory() as db:
        db.add(
            SessionRow(
                id=sid,
                driver_id=driver_id,
                manifest={"bundle_version": "v2"},
                status="ready",
                created_at=created_at,
            )
        )
        await db.commit()
    return sid


async def _seed_insight(
    factory: Any, *, driver_id: Any, session_id: Any, observation: str
) -> Any:
    iid = uuid6.uuid7()
    async with factory() as db:
        db.add(
            CoachingInsight(
                id=iid,
                session_id=session_id,
                driver_id=driver_id,
                created_at=datetime(2026, 4, 26, tzinfo=UTC),
                rule_id="rule.x.v1",
                rule_version="1",
                observation=observation,
                evidence=[],
                severity="medium",
                confidence=0.8,
            )
        )
        await db.commit()
    return iid


async def _seed_prescription(
    factory: Any, *, driver_id: Any, weakness_id: Any, title: str
) -> Any:
    pid = uuid6.uuid7()
    async with factory() as db:
        db.add(
            ActionPrescription(
                id=pid,
                driver_id=driver_id,
                weakness_episode_id=weakness_id,
                created_at=datetime(2026, 4, 26, tzinfo=UTC),
                priority="p0",
                title=title,
                rationale="...",
                drill_template_id="drill.x.v1",
                expected_context={},
                success_condition={"comparison": "gte", "target_pct": 0.85},
                status="proposed",
            )
        )
        await db.commit()
    return pid


async def _seed_weakness(
    factory: Any,
    *,
    driver_id: Any,
    sessions_affected: list[Any] | None = None,
    linked_insights: list[Any] | None = None,
    linked_prescriptions: list[Any] | None = None,
) -> Any:
    wid = uuid6.uuid7()
    async with factory() as db:
        db.add(
            WeaknessEpisode(
                id=wid,
                driver_id=driver_id,
                category="technique",
                subcategory="late_release",
                first_observed_at=datetime(2026, 4, 1, tzinfo=UTC),
                last_observed_at=datetime(2026, 4, 25, tzinfo=UTC),
                recurrence_count=3,
                sessions_affected=[
                    str(s) for s in (sessions_affected or [])
                ],
                severity="medium",
                confidence=0.7,
                status="open",
                linked_insights=[str(i) for i in (linked_insights or [])],
                linked_prescriptions=[
                    str(p) for p in (linked_prescriptions or [])
                ],
                library_rule_id="rule.x.v1",
            )
        )
        await db.commit()
    return wid


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


def _hit(client: TestClient, configured_app: dict[str, Any], who: str, wid: Any) -> Any:
    return client.get(
        f"/api/v1/driver/weaknesses/{wid}",
        headers={
            "Authorization": (
                f"Bearer {_bearer(configured_app['keys'], who)}"
            )
        },
    )


# -- behavior ------------------------------------------------------


@pytest.mark.usefixtures("configured_app")
def test_unauthenticated_returns_401() -> None:
    client = TestClient(app)
    resp = client.get(f"/api/v1/driver/weaknesses/{uuid6.uuid7()}")
    assert resp.status_code == 401


def test_unknown_weakness_id_returns_404(
    configured_app: dict[str, Any],
) -> None:
    asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_404")
    )
    client = TestClient(app)
    resp = _hit(client, configured_app, "u_404", uuid6.uuid7())
    assert resp.status_code == 404


def test_other_drivers_weakness_returns_404(
    configured_app: dict[str, Any],
) -> None:
    asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_a")
    )
    other = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_b")
    )
    wid = asyncio.run(
        _seed_weakness(
            configured_app["factory"], driver_id=other["driver_id"]
        )
    )
    client = TestClient(app)
    resp = _hit(client, configured_app, "u_a", wid)
    assert resp.status_code == 404


def test_returns_full_weakness_with_empty_sidecars(
    configured_app: dict[str, Any],
) -> None:
    """A weakness with no sessions/insights/prescriptions linked
    must still return 200 with empty arrays (not nulls / not 500)."""
    seeded = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_bare")
    )
    wid = asyncio.run(
        _seed_weakness(
            configured_app["factory"], driver_id=seeded["driver_id"]
        )
    )
    client = TestClient(app)
    body = _hit(client, configured_app, "u_bare", wid).json()
    assert body["weakness"]["id"] == str(wid)
    assert body["history"] == []
    assert body["evidence"] == []
    assert body["linked_prescriptions"] == []


def test_history_lists_sessions_with_created_at(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_h")
    )
    base = datetime(2026, 4, 26, tzinfo=UTC)
    s1 = asyncio.run(
        _seed_session(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            created_at=base - timedelta(days=2),
        )
    )
    s2 = asyncio.run(
        _seed_session(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            created_at=base,
        )
    )
    wid = asyncio.run(
        _seed_weakness(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            sessions_affected=[s1, s2],
        )
    )
    client = TestClient(app)
    body = _hit(client, configured_app, "u_h", wid).json()
    by_sid = {h["session_id"]: h for h in body["history"]}
    assert str(s1) in by_sid
    assert str(s2) in by_sid
    # session_created_at must reflect the actual seeded value.
    assert by_sid[str(s2)]["session_created_at"].startswith("2026-04-26")


def test_evidence_returns_linked_coaching_insights(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_ev")
    )
    sid = asyncio.run(
        _seed_session(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            created_at=datetime(2026, 4, 26, tzinfo=UTC),
        )
    )
    insight_id = asyncio.run(
        _seed_insight(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            session_id=sid,
            observation="late release at T1",
        )
    )
    wid = asyncio.run(
        _seed_weakness(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            linked_insights=[insight_id],
        )
    )
    client = TestClient(app)
    body = _hit(client, configured_app, "u_ev", wid).json()
    assert len(body["evidence"]) == 1
    assert body["evidence"][0]["observation"] == "late release at T1"


def test_linked_prescriptions_returns_attached_rx(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_rx")
    )
    wid = asyncio.run(
        _seed_weakness(
            configured_app["factory"], driver_id=seeded["driver_id"]
        )
    )
    pid = asyncio.run(
        _seed_prescription(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            weakness_id=wid,
            title="trail-brake drill",
        )
    )
    # Wire prescription_id into the weakness's linked_prescriptions array.
    async def _wire() -> None:
        async with configured_app["factory"]() as db:
            from sqlalchemy import select
            row = (
                await db.execute(
                    select(WeaknessEpisode).where(WeaknessEpisode.id == wid)
                )
            ).scalar_one()
            row.linked_prescriptions = [str(pid)]
            await db.commit()

    asyncio.run(_wire())
    client = TestClient(app)
    body = _hit(client, configured_app, "u_rx", wid).json()
    assert len(body["linked_prescriptions"]) == 1
    assert body["linked_prescriptions"][0]["title"] == "trail-brake drill"
