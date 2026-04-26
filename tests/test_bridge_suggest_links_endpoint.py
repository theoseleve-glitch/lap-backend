"""T046 (sprint2-backend) — GET /api/v1/bridge/suggest_links.

Given one of the driver's sessions, return other sessions that
could plausibly pair with it for bridge analysis. The pairing
filters are:

  - same master_track_id (a sim track and a real track resolve to
    the same canonical track via TrackLayout.master_track_id);
  - same car_class (driving the same kind of car);
  - within 60 days of the source session's created_at (recency).

Cross-driver isolation: the source session must belong to the
authenticated driver — same 404-not-403 rule as the rest of the
sessions surface so callers can't enumerate other drivers' ids.

Source bridgeability: if the source session is missing track_id /
car_id in its manifest, or those don't resolve in the catalog, we
return 422 ``session_unbridgeable`` — same detail as the link-create
endpoint's pre-flight, so the UI can surface a single error message
across both flows.
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


# Reference timestamp the seed function uses — picked far in the
# past so any +60d offsets stay well clear of "now".
_T0 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


async def _seed(
    factory: Any, *, clerk_user_id: str, scenario: str = "default"
) -> dict[str, Any]:
    """Seed a driver and a deterministic set of sessions covering
    the suggest endpoint's filter cases. Returns the source session
    id and the set of expected candidate ids per filter."""
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()

    master_track = uuid6.uuid7()
    other_master_track = uuid6.uuid7()

    # Two TrackLayout rows resolving to master_track (sim+real),
    # one resolving to a different master_track (excluded).
    track_same_a = uuid6.uuid7()
    track_same_b = uuid6.uuid7()
    track_other = uuid6.uuid7()

    # Two CarModel rows in class "gt3", one in class "gt4" (excluded).
    car_gt3_a = uuid6.uuid7()
    car_gt3_b = uuid6.uuid7()
    car_gt4 = uuid6.uuid7()

    source_id = uuid6.uuid7()
    cand_in_window_past = uuid6.uuid7()
    cand_in_window_future = uuid6.uuid7()
    cand_other_track = uuid6.uuid7()
    cand_other_class = uuid6.uuid7()
    cand_outside_window = uuid6.uuid7()

    async with factory() as db:
        db.add(
            User(
                id=user_id, clerk_user_id=clerk_user_id,
                email=f"{clerk_user_id}@t",
            )
        )
        db.add(DriverProfile(id=driver_id, user_id=user_id, display_name="A"))
        for i, (tid, master) in enumerate([
            (track_same_a, master_track),
            (track_same_b, master_track),
            (track_other, other_master_track),
        ]):
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
                    master_track_id=master,
                )
            )
        for i, (cid, klass) in enumerate([
            (car_gt3_a, "gt3"),
            (car_gt3_b, "gt3"),
            (car_gt4, "gt4"),
        ]):
            db.add(
                CarModel(
                    id=cid,
                    source_provider=f"prov-c-{clerk_user_id}-{i}",
                    name=f"car-{clerk_user_id}-{i}",
                    car_class=klass,
                    master_car_id=uuid6.uuid7(),
                )
            )
        # Sessions: vary one filter axis at a time relative to source.
        sessions = [
            (source_id, track_same_a, car_gt3_a, _T0),
            (cand_in_window_past, track_same_b, car_gt3_b, _T0 - timedelta(days=30)),
            (cand_in_window_future, track_same_b, car_gt3_b, _T0 + timedelta(days=10)),
            (cand_other_track, track_other, car_gt3_a, _T0),
            (cand_other_class, track_same_a, car_gt4, _T0),
            (cand_outside_window, track_same_a, car_gt3_a, _T0 - timedelta(days=90)),
        ]
        for sid, tid, cid, ts in sessions:
            manifest: dict[str, Any] = (
                {"track_id": str(tid), "car_id": str(cid)}
                if scenario != "source_unbridgeable" or sid != source_id
                else {}
            )
            db.add(
                SessionRow(
                    id=sid, driver_id=driver_id,
                    manifest=manifest, created_at=ts, status="ready",
                )
            )
        await db.commit()
    return {
        "user_id": user_id,
        "driver_id": driver_id,
        "source_id": source_id,
        "expected_candidates": {cand_in_window_past, cand_in_window_future},
        "other_track": cand_other_track,
        "other_class": cand_other_class,
        "outside_window": cand_outside_window,
    }


def _get(
    client: TestClient, ca: dict[str, Any], who: str, session_id: Any
) -> Any:
    return client.get(
        f"/api/v1/bridge/suggest_links?session_id={session_id}",
        headers={"Authorization": f"Bearer {_bearer(ca['keys'], who)}"},
    )


# -- happy path ---------------------------------------------------


def test_returns_only_same_master_same_class_within_60_days(
    configured_app: dict[str, Any],
) -> None:
    seed = asyncio.run(_seed(configured_app["factory"], clerk_user_id="alice"))
    with TestClient(app) as client:
        resp = _get(client, configured_app, "alice", seed["source_id"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["source_session_id"] == str(seed["source_id"])
    returned = {c["session_id"] for c in body["candidates"]}
    assert returned == {str(s) for s in seed["expected_candidates"]}


def test_excludes_different_track(configured_app: dict[str, Any]) -> None:
    seed = asyncio.run(_seed(configured_app["factory"], clerk_user_id="alice"))
    with TestClient(app) as client:
        resp = _get(client, configured_app, "alice", seed["source_id"])
    returned = {c["session_id"] for c in resp.json()["candidates"]}
    assert str(seed["other_track"]) not in returned


def test_excludes_different_car_class(
    configured_app: dict[str, Any],
) -> None:
    seed = asyncio.run(_seed(configured_app["factory"], clerk_user_id="alice"))
    with TestClient(app) as client:
        resp = _get(client, configured_app, "alice", seed["source_id"])
    returned = {c["session_id"] for c in resp.json()["candidates"]}
    assert str(seed["other_class"]) not in returned


def test_excludes_outside_60_day_window(
    configured_app: dict[str, Any],
) -> None:
    seed = asyncio.run(_seed(configured_app["factory"], clerk_user_id="alice"))
    with TestClient(app) as client:
        resp = _get(client, configured_app, "alice", seed["source_id"])
    returned = {c["session_id"] for c in resp.json()["candidates"]}
    assert str(seed["outside_window"]) not in returned


def test_excludes_source_session_itself(
    configured_app: dict[str, Any],
) -> None:
    """The source can't pair with itself — even though it trivially
    matches all filters."""
    seed = asyncio.run(_seed(configured_app["factory"], clerk_user_id="alice"))
    with TestClient(app) as client:
        resp = _get(client, configured_app, "alice", seed["source_id"])
    returned = {c["session_id"] for c in resp.json()["candidates"]}
    assert str(seed["source_id"]) not in returned


def test_candidate_carries_created_at(
    configured_app: dict[str, Any],
) -> None:
    """Each candidate exposes its created_at so the UI can show
    'sim from 30 days ago' in the choose-pair list."""
    seed = asyncio.run(_seed(configured_app["factory"], clerk_user_id="alice"))
    with TestClient(app) as client:
        resp = _get(client, configured_app, "alice", seed["source_id"])
    for c in resp.json()["candidates"]:
        assert "created_at" in c
        # ISO 8601 string round-trippable to datetime
        datetime.fromisoformat(c["created_at"].replace("Z", "+00:00"))


# -- isolation ----------------------------------------------------


def test_source_owned_by_other_driver_returns_404(
    configured_app: dict[str, Any],
) -> None:
    """Cross-driver isolation: a session id that exists but belongs
    to someone else returns 404, not 403, so callers can't probe
    for other drivers' session ids."""
    asyncio.run(_seed(configured_app["factory"], clerk_user_id="alice"))
    bob_seed = asyncio.run(
        _seed(configured_app["factory"], clerk_user_id="bob")
    )
    with TestClient(app) as client:
        resp = _get(client, configured_app, "alice", bob_seed["source_id"])
    assert resp.status_code == 404


def test_unknown_source_returns_404(
    configured_app: dict[str, Any],
) -> None:
    """A session_id that doesn't exist anywhere in the DB also 404s
    — same response as 'exists but owned by someone else', no
    side-channel on existence."""
    asyncio.run(_seed(configured_app["factory"], clerk_user_id="alice"))
    fake = uuid6.uuid7()
    with TestClient(app) as client:
        resp = _get(client, configured_app, "alice", fake)
    assert resp.status_code == 404


# -- source bridgeability -----------------------------------------


def test_source_missing_track_or_car_returns_422(
    configured_app: dict[str, Any],
) -> None:
    """Source session with no track_id/car_id can't ground a pairing
    — return 422 with the same `session_unbridgeable` detail used
    by the link-create endpoint."""
    seed = asyncio.run(
        _seed(
            configured_app["factory"],
            clerk_user_id="alice",
            scenario="source_unbridgeable",
        )
    )
    with TestClient(app) as client:
        resp = _get(client, configured_app, "alice", seed["source_id"])
    assert resp.status_code == 422
    assert resp.json()["detail"] == "session_unbridgeable"
