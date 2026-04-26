"""T006 (sprint2-backend) — GET /compare_cases/{id} auto-updates.

When a referenced session reprocesses (its SessionRecord row
is updated), the next GET reflects the new values. The
mechanism is "no cache" — the snapshot is recomputed on every
GET from the live SessionRecord rows.

Tests:
- baseline: GET returns snapshot matching seeded SessionRecord
- session reprocesses (SessionRecord updated) → next GET reflects it
- a referenced session with no SessionRecord shows nulls, not crash
- POST response still has sessions=null (snapshot is GET-only)
"""

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


async def _seed_driver_and_sessions(
    factory: Any,
    *,
    clerk_user_id: str,
    sessions_with_best_lap: list[int | None],
) -> dict[str, Any]:
    """Seed a driver + N sessions, each with optional SessionRecord.
    A None entry seeds the Session but no SessionRecord, simulating
    a session that hasn't been processed yet."""
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    session_ids: list[Any] = []
    async with factory() as db:
        db.add(
            User(
                id=user_id,
                clerk_user_id=clerk_user_id,
                email=f"{clerk_user_id}@t",
            )
        )
        db.add(DriverProfile(id=driver_id, user_id=user_id, display_name="A"))
        for best in sessions_with_best_lap:
            sid = uuid6.uuid7()
            session_ids.append(sid)
            db.add(
                SessionRow(
                    id=sid,
                    driver_id=driver_id,
                    manifest={"bundle_version": "v2"},
                    status="ready",
                    created_at=datetime(2026, 4, 26, tzinfo=UTC),
                )
            )
            if best is not None:
                rid = uuid6.uuid7()
                db.add(
                    SessionRecordRow(
                        id=rid,
                        session_id=sid,
                        payload={
                            "id": str(rid),
                            "session_id": str(sid),
                            "manifest": {},
                            "processed_at": "2026-04-26T12:00:00+00:00",
                            "schema_version": "v1",
                            "best_lap_ms": best,
                            "total_laps": 5,
                            "valid_laps": 5,
                        },
                    )
                )
        await db.commit()
    return {"driver_id": driver_id, "session_ids": session_ids}


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


# -- baseline snapshot --------------------------------------------


def test_get_includes_session_snapshot(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver_and_sessions(
            configured_app["factory"],
            clerk_user_id="u_snap",
            sessions_with_best_lap=[92_000, 95_500],
        )
    )
    client = TestClient(app)
    headers = {
        "Authorization": f"Bearer {_bearer(configured_app['keys'], 'u_snap')}"
    }
    created = client.post(
        "/api/v1/compare_cases",
        json={
            "session_ids": [str(s) for s in seeded["session_ids"]],
            "rationale": "x",
        },
        headers=headers,
    ).json()
    body = client.get(
        f"/api/v1/compare_cases/{created['id']}", headers=headers
    ).json()
    assert body["sessions"] is not None
    assert len(body["sessions"]) == 2
    by_sid = {s["session_id"]: s for s in body["sessions"]}
    assert by_sid[str(seeded["session_ids"][0])]["best_lap_ms"] == 92_000
    assert by_sid[str(seeded["session_ids"][1])]["best_lap_ms"] == 95_500


def test_post_response_omits_session_snapshot(
    configured_app: dict[str, Any],
) -> None:
    """POST returns sessions=None — the snapshot is a GET-time
    compute, not a create-time persisted artifact."""
    seeded = asyncio.run(
        _seed_driver_and_sessions(
            configured_app["factory"],
            clerk_user_id="u_post",
            sessions_with_best_lap=[90_000, 91_000],
        )
    )
    client = TestClient(app)
    headers = {
        "Authorization": f"Bearer {_bearer(configured_app['keys'], 'u_post')}"
    }
    created = client.post(
        "/api/v1/compare_cases",
        json={
            "session_ids": [str(s) for s in seeded["session_ids"]],
            "rationale": "x",
        },
        headers=headers,
    ).json()
    assert created["sessions"] is None


# -- auto-update --------------------------------------------------


def test_get_reflects_session_reprocess(
    configured_app: dict[str, Any],
) -> None:
    """Update a SessionRecord (simulating reprocess) → next GET
    must show the new value. This is the central T006 contract."""
    seeded = asyncio.run(
        _seed_driver_and_sessions(
            configured_app["factory"],
            clerk_user_id="u_re",
            sessions_with_best_lap=[92_000, 95_500],
        )
    )
    client = TestClient(app)
    headers = {
        "Authorization": f"Bearer {_bearer(configured_app['keys'], 'u_re')}"
    }
    created = client.post(
        "/api/v1/compare_cases",
        json={
            "session_ids": [str(s) for s in seeded["session_ids"]],
            "rationale": "x",
        },
        headers=headers,
    ).json()
    target_sid = seeded["session_ids"][0]

    # Reprocess: rewrite the SessionRecord payload with a new
    # best_lap_ms (driver shaved 4 seconds off — congrats).
    async def _reprocess() -> None:
        async with configured_app["factory"]() as db:
            row = (
                await db.execute(
                    select(SessionRecordRow).where(
                        SessionRecordRow.session_id == target_sid
                    )
                )
            ).scalar_one()
            row.payload = {
                **row.payload,
                "best_lap_ms": 88_000,
                "total_laps": 7,
                "valid_laps": 7,
            }
            await db.commit()

    asyncio.run(_reprocess())

    body = client.get(
        f"/api/v1/compare_cases/{created['id']}", headers=headers
    ).json()
    by_sid = {s["session_id"]: s for s in body["sessions"]}
    assert by_sid[str(target_sid)]["best_lap_ms"] == 88_000
    assert by_sid[str(target_sid)]["total_laps"] == 7
    # Other session unchanged.
    assert by_sid[str(seeded["session_ids"][1])]["best_lap_ms"] == 95_500


def test_get_handles_session_without_record(
    configured_app: dict[str, Any],
) -> None:
    """A session that hasn't been processed yet (no SessionRecord)
    must show nulls in its snapshot, not crash the response."""
    seeded = asyncio.run(
        _seed_driver_and_sessions(
            configured_app["factory"],
            clerk_user_id="u_pend",
            sessions_with_best_lap=[92_000, None],  # 2nd: still processing
        )
    )
    client = TestClient(app)
    headers = {
        "Authorization": f"Bearer {_bearer(configured_app['keys'], 'u_pend')}"
    }
    created = client.post(
        "/api/v1/compare_cases",
        json={
            "session_ids": [str(s) for s in seeded["session_ids"]],
            "rationale": "x",
        },
        headers=headers,
    ).json()
    body = client.get(
        f"/api/v1/compare_cases/{created['id']}", headers=headers
    ).json()
    by_sid = {s["session_id"]: s for s in body["sessions"]}
    assert by_sid[str(seeded["session_ids"][0])]["best_lap_ms"] == 92_000
    pending = by_sid[str(seeded["session_ids"][1])]
    assert pending["best_lap_ms"] is None
    assert pending["processed_at"] is None
    # session_id field stays populated even when the record is missing.
    assert pending["session_id"] == str(seeded["session_ids"][1])
