"""T026 (sprint2-backend) — bridge link validation.

POST /api/v1/bridge/links must reject session pairs that aren't
comparable: both sessions must have the same master_track_id and
the same car_class (the catalog-resolved cross-sim grouping
fields, not the per-sim track_id/car_id).

Sessions reference sim-specific `track_id` and `car_id` in their
manifest. The bridge validation walks each through the catalog
(TrackLayout, CarModel) to get `master_track_id` and `car_class`,
then compares.

Failure modes (all → 422 with a descriptive `detail`):
- different master_track_id          → "incompatible_track"
- different car_class                → "incompatible_car_class"
- a session's manifest lacks track_id/car_id  → "session_unbridgeable"
- a session's track_id/car_id has no catalog row → "session_unbridgeable"

This is enforced *after* the cross-driver isolation check (T019)
so callers can't probe for catalog state across drivers.
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


async def _seed(
    factory: Any,
    *,
    clerk_user_id: str,
    session_a_track: UUID7_LIKE,
    session_a_car: UUID7_LIKE,
    session_b_track: UUID7_LIKE,
    session_b_car: UUID7_LIKE,
    catalog: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Seed driver + 2 sessions + optional catalog rows.

    `session_*_track` / `session_*_car` go straight into each
    session's manifest. Pass None for either to omit that key from
    the manifest (lets us test the "manifest missing" branches).

    `catalog` is a list of {"kind": "track"|"car", "id": UUID,
    "master_id" or "car_class": ...} rows to add. Sessions with
    track_id / car_id pointing at IDs not in the catalog test the
    "catalog row missing" branch.
    """
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    session_a = uuid6.uuid7()
    session_b = uuid6.uuid7()
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
        # Disambiguate provider/name across catalog rows so the
        # uq_track_layouts_provider_name_layout and
        # uq_car_models_provider_name constraints don't fire when
        # a test seeds two equivalent entries.
        for i, entry in enumerate(catalog or []):
            if entry["kind"] == "track":
                db.add(
                    TrackLayout(
                        id=entry["id"],
                        source_provider=f"prov-{i}",
                        name=f"track-{i}",
                        layout_name="gp",
                        start_finish_lat=0.0,
                        start_finish_lon=0.0,
                        start_finish_heading_deg=0.0,
                        sector_checkpoints=[],
                        corner_names=[],
                        master_track_id=entry["master_id"],
                    )
                )
            elif entry["kind"] == "car":
                db.add(
                    CarModel(
                        id=entry["id"],
                        source_provider=f"prov-{i}",
                        name=f"car-{i}",
                        car_class=entry["car_class"],
                        master_car_id=uuid6.uuid7(),
                    )
                )
        manifest_a: dict[str, Any] = {}
        if session_a_track is not None:
            manifest_a["track_id"] = str(session_a_track)
        if session_a_car is not None:
            manifest_a["car_id"] = str(session_a_car)
        manifest_b: dict[str, Any] = {}
        if session_b_track is not None:
            manifest_b["track_id"] = str(session_b_track)
        if session_b_car is not None:
            manifest_b["car_id"] = str(session_b_car)
        db.add(
            SessionRow(
                id=session_a,
                driver_id=driver_id,
                manifest=manifest_a,
                created_at=datetime(2026, 4, 25, tzinfo=UTC),
                status="ready",
            )
        )
        db.add(
            SessionRow(
                id=session_b,
                driver_id=driver_id,
                manifest=manifest_b,
                created_at=datetime(2026, 4, 25, tzinfo=UTC),
                status="ready",
            )
        )
        await db.commit()
    return {
        "user_id": user_id,
        "driver_id": driver_id,
        "session_pair": [session_a, session_b],
    }


# UUID7 alias for type clarity in helper sigs (anything-uuid is fine).
UUID7_LIKE = Any


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
    client: TestClient, ca: dict[str, Any], who: str, body: dict[str, Any]
) -> Any:
    return client.post(
        "/api/v1/bridge/links",
        headers={"Authorization": f"Bearer {_bearer(ca['keys'], who)}"},
        json=body,
    )


# -- happy path: same track + class allowed -----------------------


def test_create_succeeds_when_track_and_class_match(
    configured_app: dict[str, Any],
) -> None:
    """Both sessions on the same master_track_id and same car_class
    is the canonical bridge case — must succeed. This pins the
    happy path; without it a too-strict validation would silently
    block real use cases."""
    track_a = uuid6.uuid7()
    track_b = uuid6.uuid7()
    car_a = uuid6.uuid7()
    car_b = uuid6.uuid7()
    master_track = uuid6.uuid7()
    seeded = asyncio.run(
        _seed(
            configured_app["factory"],
            clerk_user_id="u_ok",
            session_a_track=track_a,
            session_a_car=car_a,
            session_b_track=track_b,
            session_b_car=car_b,
            catalog=[
                {"kind": "track", "id": track_a, "master_id": master_track},
                {"kind": "track", "id": track_b, "master_id": master_track},
                {"kind": "car", "id": car_a, "car_class": "gt3"},
                {"kind": "car", "id": car_b, "car_class": "gt3"},
            ],
        )
    )
    client = TestClient(app)
    resp = _post(
        client, configured_app, "u_ok", _payload(seeded["session_pair"])
    )
    assert resp.status_code == 201, resp.text


# -- failure cases ------------------------------------------------


def test_create_rejects_different_master_track_id(
    configured_app: dict[str, Any],
) -> None:
    track_a = uuid6.uuid7()
    track_b = uuid6.uuid7()
    car_a = uuid6.uuid7()
    car_b = uuid6.uuid7()
    seeded = asyncio.run(
        _seed(
            configured_app["factory"],
            clerk_user_id="u_t",
            session_a_track=track_a,
            session_a_car=car_a,
            session_b_track=track_b,
            session_b_car=car_b,
            catalog=[
                {"kind": "track", "id": track_a, "master_id": uuid6.uuid7()},
                {"kind": "track", "id": track_b, "master_id": uuid6.uuid7()},
                {"kind": "car", "id": car_a, "car_class": "gt3"},
                {"kind": "car", "id": car_b, "car_class": "gt3"},
            ],
        )
    )
    client = TestClient(app)
    resp = _post(
        client, configured_app, "u_t", _payload(seeded["session_pair"])
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "incompatible_track"


def test_create_rejects_different_car_class(
    configured_app: dict[str, Any],
) -> None:
    track_a = uuid6.uuid7()
    track_b = uuid6.uuid7()
    car_a = uuid6.uuid7()
    car_b = uuid6.uuid7()
    master_track = uuid6.uuid7()
    seeded = asyncio.run(
        _seed(
            configured_app["factory"],
            clerk_user_id="u_c",
            session_a_track=track_a,
            session_a_car=car_a,
            session_b_track=track_b,
            session_b_car=car_b,
            catalog=[
                {"kind": "track", "id": track_a, "master_id": master_track},
                {"kind": "track", "id": track_b, "master_id": master_track},
                {"kind": "car", "id": car_a, "car_class": "gt3"},
                {"kind": "car", "id": car_b, "car_class": "gt4"},
            ],
        )
    )
    client = TestClient(app)
    resp = _post(
        client, configured_app, "u_c", _payload(seeded["session_pair"])
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "incompatible_car_class"


def test_create_rejects_session_missing_track_id_in_manifest(
    configured_app: dict[str, Any],
) -> None:
    """A session whose manifest lacks track_id can't be bridged —
    we have nothing to compare against. Refuse rather than guess
    or skip the check."""
    car_a = uuid6.uuid7()
    car_b = uuid6.uuid7()
    seeded = asyncio.run(
        _seed(
            configured_app["factory"],
            clerk_user_id="u_m",
            session_a_track=None,  # missing
            session_a_car=car_a,
            session_b_track=uuid6.uuid7(),
            session_b_car=car_b,
            catalog=[
                {"kind": "car", "id": car_a, "car_class": "gt3"},
                {"kind": "car", "id": car_b, "car_class": "gt3"},
            ],
        )
    )
    client = TestClient(app)
    resp = _post(
        client, configured_app, "u_m", _payload(seeded["session_pair"])
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "session_unbridgeable"


def test_create_rejects_session_with_unknown_track_in_catalog(
    configured_app: dict[str, Any],
) -> None:
    """A session pointing at a track_id with no catalog row can't
    be bridged — we can't resolve master_track_id."""
    track_known = uuid6.uuid7()
    car = uuid6.uuid7()
    master_track = uuid6.uuid7()
    seeded = asyncio.run(
        _seed(
            configured_app["factory"],
            clerk_user_id="u_x",
            session_a_track=track_known,
            session_a_car=car,
            session_b_track=uuid6.uuid7(),  # not in catalog
            session_b_car=car,
            catalog=[
                {
                    "kind": "track",
                    "id": track_known,
                    "master_id": master_track,
                },
                {"kind": "car", "id": car, "car_class": "gt3"},
            ],
        )
    )
    client = TestClient(app)
    resp = _post(
        client, configured_app, "u_x", _payload(seeded["session_pair"])
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "session_unbridgeable"
