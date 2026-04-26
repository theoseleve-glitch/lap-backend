"""T030 (sprint2-backend) — bulk bridge link create.

POST /api/v1/bridge/links/bulk creates multiple TransferLinks in a
single transaction. Migration/import use case: one driver's batch
of links in one trip. Atomic — all succeed or all fail; if any
item fails cross-driver, validation, or dedup, the whole transaction
rolls back. The response carries the created links in input order
so callers correlate by index.

Each item runs the same checks as the single POST: T026 validation
(same master_track_id + same car_class), T027 dedup (no duplicate
pairs — including within the batch itself), T028 generates a
CoachingInsight per link.
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
from sqlalchemy import select

from alembic import command
from app.deps import get_db, get_jwks_client
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import (
    CarModel,
    CoachingInsight,
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


async def _seed_driver_with_n_sessions(
    factory: Any, *, clerk_user_id: str, n: int
) -> dict[str, Any]:
    """Seed a driver + n bridgeable sessions: shared
    master_track_id, shared car_class — all pairs among them pass
    the T026 validator."""
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    session_ids = [uuid6.uuid7() for _ in range(n)]
    track_ids = [uuid6.uuid7() for _ in range(n)]
    car_ids = [uuid6.uuid7() for _ in range(n)]
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


def _item(session_pair: list[Any]) -> dict[str, Any]:
    return {
        "created_by": "auto",
        "session_pair": [str(s) for s in session_pair],
        "link_reason": "same_track",
        "shape_match_score": 0.78,
        "intensity_match_score": 0.62,
        "divergence_points": [],
        "transferred_skills": [],
        "non_transferred_skills": [],
        "generated_report_id": None,
    }


def _post_bulk(
    client: TestClient,
    ca: dict[str, Any],
    who: str,
    items: list[dict[str, Any]],
) -> Any:
    return client.post(
        "/api/v1/bridge/links/bulk",
        headers={"Authorization": f"Bearer {_bearer(ca['keys'], who)}"},
        json={"items": items},
    )


# -- behavior ------------------------------------------------------


def test_bulk_create_returns_201_with_all_created_links(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver_with_n_sessions(
            configured_app["factory"], clerk_user_id="u_b", n=4
        )
    )
    sids = seeded["session_ids"]
    items = [_item([sids[0], sids[1]]), _item([sids[2], sids[3]])]
    client = TestClient(app)
    resp = _post_bulk(client, configured_app, "u_b", items)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert len(body["items"]) == 2
    # Server-set fields populated on each item.
    for item in body["items"]:
        assert item["driver_id"] == str(seeded["driver_id"])
        assert item["id"]
        assert item["created_at"]


def test_bulk_create_persists_all_link_rows(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver_with_n_sessions(
            configured_app["factory"], clerk_user_id="u_p", n=4
        )
    )
    sids = seeded["session_ids"]
    items = [_item([sids[0], sids[1]]), _item([sids[2], sids[3]])]
    client = TestClient(app)
    _post_bulk(client, configured_app, "u_p", items)

    async def _read() -> int:
        async with configured_app["factory"]() as db:
            rows = (
                (
                    await db.execute(
                        select(TransferLink).where(
                            TransferLink.driver_id == seeded["driver_id"]
                        )
                    )
                )
                .scalars()
                .all()
            )
            return len(list(rows))

    assert asyncio.run(_read()) == 2


def test_bulk_create_generates_one_insight_per_link(
    configured_app: dict[str, Any],
) -> None:
    """T028 carries through to bulk: each created link gets its
    own bridge.link_created CoachingInsight."""
    seeded = asyncio.run(
        _seed_driver_with_n_sessions(
            configured_app["factory"], clerk_user_id="u_i", n=4
        )
    )
    sids = seeded["session_ids"]
    items = [_item([sids[0], sids[1]]), _item([sids[2], sids[3]])]
    client = TestClient(app)
    _post_bulk(client, configured_app, "u_i", items)

    async def _read() -> int:
        async with configured_app["factory"]() as db:
            rows = (
                (
                    await db.execute(
                        select(CoachingInsight).where(
                            CoachingInsight.driver_id
                            == seeded["driver_id"]
                        )
                    )
                )
                .scalars()
                .all()
            )
            return len(list(rows))

    assert asyncio.run(_read()) == 2


def test_bulk_rejects_within_batch_duplicate_pair(
    configured_app: dict[str, Any],
) -> None:
    """Two items in the same batch with the same session_pair must
    fail. Without this check, a 100-item batch could silently drop
    duplicates — a real risk during migration."""
    seeded = asyncio.run(
        _seed_driver_with_n_sessions(
            configured_app["factory"], clerk_user_id="u_w", n=2
        )
    )
    sids = seeded["session_ids"]
    items = [_item([sids[0], sids[1]]), _item([sids[1], sids[0]])]
    client = TestClient(app)
    resp = _post_bulk(client, configured_app, "u_w", items)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "duplicate_link"


def test_bulk_rejects_pair_already_in_db(
    configured_app: dict[str, Any],
) -> None:
    """Existing-link dedup carries through. If a batch item
    duplicates a pair already linked in the DB, the whole batch
    rolls back (atomic)."""
    seeded = asyncio.run(
        _seed_driver_with_n_sessions(
            configured_app["factory"], clerk_user_id="u_e", n=4
        )
    )
    sids = seeded["session_ids"]
    client = TestClient(app)
    # Pre-existing link via single endpoint.
    pre = client.post(
        "/api/v1/bridge/links",
        headers={
            "Authorization": f"Bearer {_bearer(configured_app['keys'], 'u_e')}"
        },
        json=_item([sids[0], sids[1]]),
    )
    assert pre.status_code == 201
    # Now bulk: one fresh pair + the existing pair → conflict, atomic rollback.
    items = [_item([sids[2], sids[3]]), _item([sids[0], sids[1]])]
    resp = _post_bulk(client, configured_app, "u_e", items)
    assert resp.status_code == 409

    # Atomic: the fresh pair must NOT have been inserted.
    async def _count() -> int:
        async with configured_app["factory"]() as db:
            rows = (
                (
                    await db.execute(
                        select(TransferLink).where(
                            TransferLink.driver_id == seeded["driver_id"]
                        )
                    )
                )
                .scalars()
                .all()
            )
            return len(list(rows))

    assert asyncio.run(_count()) == 1


def test_bulk_rejects_session_not_owned_by_driver(
    configured_app: dict[str, Any],
) -> None:
    """If any item references a session that doesn't belong to the
    authenticated driver, the whole batch is 404. Atomic rollback
    — no items inserted."""
    a = asyncio.run(
        _seed_driver_with_n_sessions(
            configured_app["factory"], clerk_user_id="u_a", n=2
        )
    )
    b = asyncio.run(
        _seed_driver_with_n_sessions(
            configured_app["factory"], clerk_user_id="u_b", n=2
        )
    )
    items = [
        _item([a["session_ids"][0], a["session_ids"][1]]),
        _item([b["session_ids"][0], b["session_ids"][1]]),
    ]
    client = TestClient(app)
    resp = _post_bulk(client, configured_app, "u_a", items)
    assert resp.status_code == 404


def test_bulk_rejects_empty_items(
    configured_app: dict[str, Any],
) -> None:
    """Pydantic enforces min_length=1 on the schema — surfaces as
    a 422 from FastAPI's body validation."""
    client = TestClient(app)
    resp = _post_bulk(client, configured_app, "u_z", [])
    assert resp.status_code == 422


@pytest.mark.usefixtures("configured_app")
def test_bulk_unauthenticated_returns_401() -> None:
    client = TestClient(app)
    resp = client.post(
        "/api/v1/bridge/links/bulk",
        json={"items": [_item([uuid6.uuid7(), uuid6.uuid7()])]},
    )
    assert resp.status_code == 401
