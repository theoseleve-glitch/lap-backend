"""T010 (sprint2-backend) — GET /api/v1/sessions/{id}/coaching.

Composite per-session coaching view: insights + weaknesses
touched + prescriptions generated. Cross-driver isolation
mirrors `sessions.get_session` (404 for absent or not-owned).
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
    ActionPrescription,
    CoachingInsight,
    DriverProfile,
    User,
    WeaknessEpisode,
)
from infra.orm import (
    Session as SessionRow,
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


async def _seed_session(
    factory: Any, *, clerk_user_id: str
) -> dict[str, Any]:
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    sid = uuid6.uuid7()
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
        db.add(
            SessionRow(
                id=sid,
                driver_id=driver_id,
                manifest={},
                created_at=datetime(2026, 4, 25, tzinfo=UTC),
                status="ready",
            )
        )
        await db.commit()
    return {"user_id": user_id, "driver_id": driver_id, "session_id": sid}


async def _seed_insight(
    factory: Any,
    *,
    session_id: Any,
    driver_id: Any,
    suggested_prescription_id: Any | None = None,
    rule_id: str = "rule.x.v1",
) -> Any:
    iid = uuid6.uuid7()
    async with factory() as db:
        db.add(
            CoachingInsight(
                id=iid,
                session_id=session_id,
                driver_id=driver_id,
                created_at=datetime(2026, 4, 25, tzinfo=UTC),
                rule_id=rule_id,
                rule_version="1",
                observation="late release",
                evidence=[],
                severity="medium",
                confidence=0.8,
                suggested_prescription_id=suggested_prescription_id,
            )
        )
        await db.commit()
    return iid


async def _seed_prescription(
    factory: Any, *, driver_id: Any, prescription_id: Any | None = None
) -> Any:
    pid = prescription_id if prescription_id is not None else uuid6.uuid7()
    async with factory() as db:
        db.add(
            ActionPrescription(
                id=pid,
                driver_id=driver_id,
                weakness_episode_id=None,
                created_at=datetime(2026, 4, 26, tzinfo=UTC),
                priority="p0",
                title="Practice trail-braking",
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
    linked_insight_ids: list[Any],
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
                recurrence_count=1,
                severity="medium",
                confidence=0.7,
                status="open",
                linked_insights=[str(i) for i in linked_insight_ids],
                library_rule_id="rule.x.v1",
            )
        )
        await db.commit()
    return wid


def _get(client: TestClient, ca: dict[str, Any], who: str, sid: Any) -> Any:
    return client.get(
        f"/api/v1/sessions/{sid}/coaching",
        headers={"Authorization": f"Bearer {_bearer(ca['keys'], who)}"},
    )


# -- behavior ------------------------------------------------------


def test_returns_empty_view_for_session_with_no_findings(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_session(configured_app["factory"], clerk_user_id="u_e")
    )
    client = TestClient(app)
    resp = _get(client, configured_app, "u_e", seeded["session_id"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == str(seeded["session_id"])
    assert body["insights"] == []
    assert body["weaknesses_touched"] == []
    assert body["prescriptions_generated"] == []


def test_returns_session_insights(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_session(configured_app["factory"], clerk_user_id="u_i")
    )
    asyncio.run(
        _seed_insight(
            configured_app["factory"],
            session_id=seeded["session_id"],
            driver_id=seeded["driver_id"],
        )
    )
    asyncio.run(
        _seed_insight(
            configured_app["factory"],
            session_id=seeded["session_id"],
            driver_id=seeded["driver_id"],
            rule_id="rule.y.v1",
        )
    )
    client = TestClient(app)
    resp = _get(client, configured_app, "u_i", seeded["session_id"])
    assert resp.status_code == 200
    assert len(resp.json()["insights"]) == 2


def test_excludes_insights_from_other_sessions(
    configured_app: dict[str, Any],
) -> None:
    """Only insights whose session_id matches the requested session."""
    seeded = asyncio.run(
        _seed_session(configured_app["factory"], clerk_user_id="u_o")
    )
    other_sid = uuid6.uuid7()
    asyncio.run(
        _seed_insight(
            configured_app["factory"],
            session_id=other_sid,
            driver_id=seeded["driver_id"],
        )
    )
    client = TestClient(app)
    resp = _get(client, configured_app, "u_o", seeded["session_id"])
    assert resp.status_code == 200
    assert resp.json()["insights"] == []


def test_returns_prescriptions_referenced_by_insights(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_session(configured_app["factory"], clerk_user_id="u_p")
    )
    pid = asyncio.run(
        _seed_prescription(
            configured_app["factory"], driver_id=seeded["driver_id"]
        )
    )
    asyncio.run(
        _seed_insight(
            configured_app["factory"],
            session_id=seeded["session_id"],
            driver_id=seeded["driver_id"],
            suggested_prescription_id=pid,
        )
    )
    client = TestClient(app)
    resp = _get(client, configured_app, "u_p", seeded["session_id"])
    assert resp.status_code == 200
    body = resp.json()
    rx = body["prescriptions_generated"]
    assert len(rx) == 1
    assert rx[0]["id"] == str(pid)


def test_skips_insights_without_suggested_prescription(
    configured_app: dict[str, Any],
) -> None:
    """An insight with no suggested_prescription_id contributes
    nothing to prescriptions_generated."""
    seeded = asyncio.run(
        _seed_session(configured_app["factory"], clerk_user_id="u_np")
    )
    asyncio.run(
        _seed_insight(
            configured_app["factory"],
            session_id=seeded["session_id"],
            driver_id=seeded["driver_id"],
            suggested_prescription_id=None,
        )
    )
    client = TestClient(app)
    resp = _get(client, configured_app, "u_np", seeded["session_id"])
    assert resp.status_code == 200
    assert resp.json()["prescriptions_generated"] == []


def test_returns_weaknesses_linked_to_session_insights(
    configured_app: dict[str, Any],
) -> None:
    """A WeaknessEpisode whose linked_insights contains an insight
    from this session shows up in weaknesses_touched."""
    seeded = asyncio.run(
        _seed_session(configured_app["factory"], clerk_user_id="u_w")
    )
    iid = asyncio.run(
        _seed_insight(
            configured_app["factory"],
            session_id=seeded["session_id"],
            driver_id=seeded["driver_id"],
        )
    )
    asyncio.run(
        _seed_weakness(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            linked_insight_ids=[iid],
        )
    )
    client = TestClient(app)
    resp = _get(client, configured_app, "u_w", seeded["session_id"])
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["weaknesses_touched"]) == 1


def test_excludes_weaknesses_unrelated_to_session(
    configured_app: dict[str, Any],
) -> None:
    """A weakness linked only to insights from a different session
    must not appear in this session's view."""
    seeded = asyncio.run(
        _seed_session(configured_app["factory"], clerk_user_id="u_uw")
    )
    other_sid = uuid6.uuid7()
    other_iid = asyncio.run(
        _seed_insight(
            configured_app["factory"],
            session_id=other_sid,
            driver_id=seeded["driver_id"],
        )
    )
    asyncio.run(
        _seed_weakness(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            linked_insight_ids=[other_iid],
        )
    )
    client = TestClient(app)
    resp = _get(client, configured_app, "u_uw", seeded["session_id"])
    assert resp.status_code == 200
    assert resp.json()["weaknesses_touched"] == []


def test_session_not_owned_returns_404(
    configured_app: dict[str, Any],
) -> None:
    other = asyncio.run(
        _seed_session(configured_app["factory"], clerk_user_id="u_other")
    )
    asyncio.run(
        _seed_session(configured_app["factory"], clerk_user_id="u_me")
    )
    client = TestClient(app)
    resp = _get(client, configured_app, "u_me", other["session_id"])
    assert resp.status_code == 404


def test_unknown_session_returns_404(
    configured_app: dict[str, Any],
) -> None:
    asyncio.run(
        _seed_session(configured_app["factory"], clerk_user_id="u_x")
    )
    client = TestClient(app)
    resp = _get(client, configured_app, "u_x", uuid6.uuid7())
    assert resp.status_code == 404


@pytest.mark.usefixtures("configured_app")
def test_unauthenticated_returns_401() -> None:
    client = TestClient(app)
    resp = client.get(f"/api/v1/sessions/{uuid6.uuid7()}/coaching")
    assert resp.status_code == 401
