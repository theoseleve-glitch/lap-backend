"""T001 (sprint2-backend) — GET /api/v1/driver/timeline.

Paginated list of the authenticated driver's session records,
newest first, with achievement badges (e.g. "personal_best") on
applicable rows.

Cursor format is opaque per the contract — T002 will refactor
the encoding to a hashed token; T001 just locks the wire shape
(string token in / out, null = no more pages) so the encoding
swap doesn't break clients.
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
from infra.orm import DriverProfile, User
from infra.orm import Session as SessionRow
from infra.orm import SessionRecord as SessionRecordRow

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
    factory: Any, *, clerk_user_id: str, email: str
) -> dict[str, Any]:
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    async with factory() as db:
        db.add(User(id=user_id, clerk_user_id=clerk_user_id, email=email))
        db.add(DriverProfile(id=driver_id, user_id=user_id, display_name="A"))
        await db.commit()
    return {"user_id": user_id, "driver_id": driver_id}


async def _seed_session_with_record(
    factory: Any,
    *,
    driver_id: Any,
    created_at: datetime,
    best_lap_ms: int | None,
    total_laps: int = 5,
    valid_laps: int = 5,
) -> Any:
    """Seed one Session + matching SessionRecord row.

    The record's payload follows the lap-schemas SessionRecord
    shape so the timeline endpoint can extract per-row aggregates
    from a single JSON column without an Alembic migration."""
    session_id = uuid6.uuid7()
    record_id = uuid6.uuid7()
    async with factory() as db:
        db.add(
            SessionRow(
                id=session_id,
                driver_id=driver_id,
                manifest={"bundle_version": "v2"},
                status="ready",
                created_at=created_at,
            )
        )
        db.add(
            SessionRecordRow(
                id=record_id,
                session_id=session_id,
                payload={
                    "id": str(record_id),
                    "session_id": str(session_id),
                    "manifest": {},
                    "processed_at": created_at.isoformat(),
                    "schema_version": "v1",
                    "best_lap_ms": best_lap_ms,
                    "total_laps": total_laps,
                    "valid_laps": valid_laps,
                },
            )
        )
        await db.commit()
    return session_id


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


# -- behavior ------------------------------------------------------


@pytest.mark.usefixtures("configured_app")
def test_timeline_without_auth_returns_401() -> None:
    client = TestClient(app)
    resp = client.get("/api/v1/driver/timeline")
    assert resp.status_code == 401


def test_empty_timeline_returns_no_entries(
    configured_app: dict[str, Any],
) -> None:
    asyncio.run(
        _seed_driver(
            configured_app["factory"],
            clerk_user_id="u_empty",
            email="empty@t",
        )
    )
    client = TestClient(app)
    token = _bearer(configured_app["keys"], "u_empty")
    resp = client.get(
        "/api/v1/driver/timeline",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["entries"] == []
    assert body["next_cursor"] is None


def test_timeline_returns_sessions_newest_first(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver(
            configured_app["factory"], clerk_user_id="u_newest", email="n@t"
        )
    )
    base = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    older = asyncio.run(
        _seed_session_with_record(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            created_at=base,
            best_lap_ms=95_000,
        )
    )
    newer = asyncio.run(
        _seed_session_with_record(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            created_at=base + timedelta(hours=1),
            best_lap_ms=92_000,
        )
    )
    client = TestClient(app)
    resp = client.get(
        "/api/v1/driver/timeline",
        headers={
            "Authorization": (
                f"Bearer {_bearer(configured_app['keys'], 'u_newest')}"
            )
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    ids = [e["session_id"] for e in body["entries"]]
    assert ids == [str(newer), str(older)]


def test_timeline_excludes_other_drivers_sessions(
    configured_app: dict[str, Any],
) -> None:
    """Cross-driver isolation: caller A must never see caller B's
    sessions even though they're in the same table."""
    me = asyncio.run(
        _seed_driver(
            configured_app["factory"], clerk_user_id="u_me", email="me@t"
        )
    )
    other = asyncio.run(
        _seed_driver(
            configured_app["factory"], clerk_user_id="u_other", email="other@t"
        )
    )
    base = datetime(2026, 4, 26, tzinfo=UTC)
    asyncio.run(
        _seed_session_with_record(
            configured_app["factory"],
            driver_id=me["driver_id"],
            created_at=base,
            best_lap_ms=92_000,
        )
    )
    asyncio.run(
        _seed_session_with_record(
            configured_app["factory"],
            driver_id=other["driver_id"],
            created_at=base,
            best_lap_ms=88_000,
        )
    )
    client = TestClient(app)
    resp = client.get(
        "/api/v1/driver/timeline",
        headers={
            "Authorization": (
                f"Bearer {_bearer(configured_app['keys'], 'u_me')}"
            )
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["entries"]) == 1, (
        "saw other driver's session in the response"
    )


def test_personal_best_badge_on_fastest_lap(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver(
            configured_app["factory"], clerk_user_id="u_pb", email="pb@t"
        )
    )
    base = datetime(2026, 4, 26, tzinfo=UTC)
    slower = asyncio.run(
        _seed_session_with_record(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            created_at=base,
            best_lap_ms=95_000,
        )
    )
    fastest = asyncio.run(
        _seed_session_with_record(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            created_at=base + timedelta(hours=1),
            best_lap_ms=88_500,  # PB
        )
    )
    middle = asyncio.run(
        _seed_session_with_record(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            created_at=base + timedelta(hours=2),
            best_lap_ms=92_000,
        )
    )
    client = TestClient(app)
    resp = client.get(
        "/api/v1/driver/timeline",
        headers={
            "Authorization": (
                f"Bearer {_bearer(configured_app['keys'], 'u_pb')}"
            )
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    by_id = {e["session_id"]: e for e in body["entries"]}
    assert "personal_best" in by_id[str(fastest)]["badges"]
    assert "personal_best" not in by_id[str(slower)]["badges"]
    assert "personal_best" not in by_id[str(middle)]["badges"]


def test_pagination_advances_via_cursor(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver(
            configured_app["factory"], clerk_user_id="u_page", email="p@t"
        )
    )
    base = datetime(2026, 4, 26, tzinfo=UTC)
    # Seed 5 sessions, ask for limit=2 — expect 3 pages (2,2,1).
    for i in range(5):
        asyncio.run(
            _seed_session_with_record(
                configured_app["factory"],
                driver_id=seeded["driver_id"],
                created_at=base + timedelta(hours=i),
                best_lap_ms=90_000 + i * 100,
            )
        )
    client = TestClient(app)
    headers = {
        "Authorization": (
            f"Bearer {_bearer(configured_app['keys'], 'u_page')}"
        )
    }

    page1 = client.get(
        "/api/v1/driver/timeline?limit=2", headers=headers
    ).json()
    assert len(page1["entries"]) == 2
    assert page1["next_cursor"] is not None

    page2 = client.get(
        f"/api/v1/driver/timeline?limit=2&cursor={page1['next_cursor']}",
        headers=headers,
    ).json()
    assert len(page2["entries"]) == 2
    assert page2["next_cursor"] is not None
    # Pages must not overlap.
    page1_ids = {e["session_id"] for e in page1["entries"]}
    page2_ids = {e["session_id"] for e in page2["entries"]}
    assert page1_ids.isdisjoint(page2_ids)

    page3 = client.get(
        f"/api/v1/driver/timeline?limit=2&cursor={page2['next_cursor']}",
        headers=headers,
    ).json()
    assert len(page3["entries"]) == 1
    assert page3["next_cursor"] is None


def test_default_limit_is_twenty(
    configured_app: dict[str, Any],
) -> None:
    """Default limit=20 per the sprint contract. Pin it so a
    future "let's lower the default" edit fails the test."""
    seeded = asyncio.run(
        _seed_driver(
            configured_app["factory"], clerk_user_id="u_dlim", email="d@t"
        )
    )
    base = datetime(2026, 4, 26, tzinfo=UTC)
    for i in range(25):
        asyncio.run(
            _seed_session_with_record(
                configured_app["factory"],
                driver_id=seeded["driver_id"],
                created_at=base + timedelta(minutes=i),
                best_lap_ms=90_000 + i,
            )
        )
    client = TestClient(app)
    resp = client.get(
        "/api/v1/driver/timeline",
        headers={
            "Authorization": (
                f"Bearer {_bearer(configured_app['keys'], 'u_dlim')}"
            )
        },
    )
    body = resp.json()
    assert len(body["entries"]) == 20
    assert body["next_cursor"] is not None
