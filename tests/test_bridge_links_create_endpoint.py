"""T019 (sprint2-backend) — POST /api/v1/bridge/links create.

Lets a driver register a sim↔real link between two of their
sessions (manual pairing) or persist an auto-detected suggestion
they accepted. Server fills id (UUID7), created_at (UTC now), and
driver_id (from the authenticated user); the rest of the
TransferLink contract is client-supplied.

Cross-driver isolation: every session_id in the pair must belong
to the authenticated driver. Mismatch → 404 (not 403) so callers
can't enumerate other drivers' session ids.
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
    DriverProfile,
    TrackLayout,
    TransferLink,
    User,
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
            "sub": clerk_user_id,
            "email": f"{clerk_user_id}@t",
            "exp": int(time.time()) + 3600,
        },
        keys["kid"],
    )


async def _seed_driver_with_sessions(
    factory: Any, *, clerk_user_id: str, n_sessions: int = 2
) -> dict[str, Any]:
    """Seed driver + N sessions all on the same master_track_id and
    same car_class so the T026 bridge validation accepts them.

    Each session points at a distinct sim-specific track_id /
    car_id (real life: iRacing-Spa vs ACC-Spa), but the catalog
    rows resolve them to a shared master_track_id and shared
    car_class — i.e., the canonical "bridgeable" pair.
    """
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    session_ids = [uuid6.uuid7() for _ in range(n_sessions)]
    track_ids = [uuid6.uuid7() for _ in range(n_sessions)]
    car_ids = [uuid6.uuid7() for _ in range(n_sessions)]
    master_track = uuid6.uuid7()
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
        for i, tid in enumerate(track_ids):
            db.add(
                TrackLayout(
                    id=tid,
                    source_provider=f"prov-t-{clerk_user_id}-{i}",
                    name=f"track-{clerk_user_id}-{i}",
                    layout_name="gp",
                    start_finish_lat=0.0,
                    start_finish_lon=0.0,
                    start_finish_heading_deg=0.0,
                    sector_checkpoints=[],
                    corner_names=[],
                    master_track_id=master_track,
                )
            )
        for i, cid in enumerate(car_ids):
            db.add(
                CarModel(
                    id=cid,
                    source_provider=f"prov-c-{clerk_user_id}-{i}",
                    name=f"car-{clerk_user_id}-{i}",
                    car_class="gt3",
                    master_car_id=uuid6.uuid7(),
                )
            )
        for sid, tid, cid in zip(
            session_ids, track_ids, car_ids, strict=True
        ):
            db.add(
                SessionRow(
                    id=sid,
                    driver_id=driver_id,
                    manifest={"track_id": str(tid), "car_id": str(cid)},
                    created_at=datetime(2026, 4, 25, tzinfo=UTC),
                    status="ready",
                )
            )
        await db.commit()
    return {
        "user_id": user_id,
        "driver_id": driver_id,
        "session_ids": session_ids,
    }


def _payload(session_ids: list[Any], **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "created_by": "auto",
        "session_pair": [str(s) for s in session_ids],
        "link_reason": "same_track",
        "shape_match_score": 0.78,
        "intensity_match_score": 0.62,
        "divergence_points": [],
        "transferred_skills": [],
        "non_transferred_skills": [],
        "generated_report_id": None,
    }
    base.update(overrides)
    return base


def _post(
    client: TestClient,
    ca: dict[str, Any],
    who: str,
    body: dict[str, Any],
) -> Any:
    return client.post(
        "/api/v1/bridge/links",
        headers={"Authorization": f"Bearer {_bearer(ca['keys'], who)}"},
        json=body,
    )


# -- behavior ------------------------------------------------------


def test_create_returns_201_with_full_transfer_link(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver_with_sessions(
            configured_app["factory"], clerk_user_id="u_c"
        )
    )
    client = TestClient(app)
    resp = _post(
        client, configured_app, "u_c", _payload(seeded["session_ids"])
    )
    assert resp.status_code == 201
    body = resp.json()
    # Server-set fields present and well-formed
    assert "id" in body
    assert "created_at" in body
    assert body["driver_id"] == str(seeded["driver_id"])
    # Echo of client-supplied fields
    assert body["created_by"] == "auto"
    assert body["link_reason"] == "same_track"
    assert body["shape_match_score"] == 0.78


def test_create_persists_row_in_db(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver_with_sessions(
            configured_app["factory"], clerk_user_id="u_p"
        )
    )
    client = TestClient(app)
    resp = _post(
        client, configured_app, "u_p", _payload(seeded["session_ids"])
    )
    link_id = resp.json()["id"]

    async def _read() -> Any:
        from sqlalchemy import select
        async with configured_app["factory"]() as db:
            row = (
                await db.execute(
                    select(TransferLink).where(
                        TransferLink.id == uuid6.UUID(link_id)
                    )
                )
            ).scalar_one()
            return {
                "driver_id": row.driver_id,
                "session_pair": row.session_pair,
                "shape_match_score": row.shape_match_score,
            }

    persisted = asyncio.run(_read())
    assert persisted["driver_id"] == seeded["driver_id"]
    assert persisted["shape_match_score"] == 0.78


def test_create_supports_manual_pairing(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver_with_sessions(
            configured_app["factory"], clerk_user_id="u_m"
        )
    )
    client = TestClient(app)
    resp = _post(
        client,
        configured_app,
        "u_m",
        _payload(
            seeded["session_ids"],
            created_by="user",
            link_reason="manual",
        ),
    )
    assert resp.status_code == 201
    assert resp.json()["created_by"] == "user"


def test_create_rejects_session_not_owned_by_driver(
    configured_app: dict[str, Any],
) -> None:
    """If any session in the pair belongs to another driver, the
    server returns 404 — same posture as get_session, so callers
    can't enumerate other drivers' session ids."""
    other = asyncio.run(
        _seed_driver_with_sessions(
            configured_app["factory"], clerk_user_id="u_other"
        )
    )
    me = asyncio.run(
        _seed_driver_with_sessions(
            configured_app["factory"], clerk_user_id="u_me"
        )
    )
    mixed = [me["session_ids"][0], other["session_ids"][0]]
    client = TestClient(app)
    resp = _post(client, configured_app, "u_me", _payload(mixed))
    assert resp.status_code == 404


def test_create_rejects_unknown_session_id(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver_with_sessions(
            configured_app["factory"], clerk_user_id="u_unk"
        )
    )
    bogus = [seeded["session_ids"][0], uuid6.uuid7()]
    client = TestClient(app)
    resp = _post(client, configured_app, "u_unk", _payload(bogus))
    assert resp.status_code == 404


def test_create_rejects_single_session_pair(
    configured_app: dict[str, Any],
) -> None:
    """min_length=2 in TransferLinkCreate; FastAPI surfaces as 422."""
    seeded = asyncio.run(
        _seed_driver_with_sessions(
            configured_app["factory"], clerk_user_id="u_one"
        )
    )
    client = TestClient(app)
    resp = _post(
        client, configured_app, "u_one", _payload(seeded["session_ids"][:1])
    )
    assert resp.status_code == 422


@pytest.mark.usefixtures("configured_app")
def test_create_unauthenticated_returns_401() -> None:
    client = TestClient(app)
    resp = client.post(
        "/api/v1/bridge/links",
        json=_payload([uuid6.uuid7(), uuid6.uuid7()]),
    )
    assert resp.status_code == 401
