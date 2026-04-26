"""T027 (sprint2-backend) — bridge link dedup.

POST /api/v1/bridge/links rejects a duplicate pair for the same
driver: the same set of session ids can't be linked twice. The
comparison is order-independent (the pair is conceptually a set,
not a sequence) and driver-scoped (driver B can link the same
pair that driver A already linked).

Failure: HTTP 409 with detail "duplicate_link". 409 (vs. 422)
because the request is well-formed and the conflict is with
existing server state — that's the textbook 409 case.
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


async def _seed_driver_with_bridgeable_pair(
    factory: Any, *, clerk_user_id: str
) -> dict[str, Any]:
    """Seed a driver + 2 sessions that pass the T026 validator
    (same master_track_id + same car_class), so any 409 we observe
    is the dedup check tripping, not the validator."""
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    session_a = uuid6.uuid7()
    session_b = uuid6.uuid7()
    track_a = uuid6.uuid7()
    track_b = uuid6.uuid7()
    car_a = uuid6.uuid7()
    car_b = uuid6.uuid7()
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
        for i, (tid, cid) in enumerate(
            [(track_a, car_a), (track_b, car_b)]
        ):
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
            db.add(
                CarModel(
                    id=cid,
                    source_provider=f"prov-c-{clerk_user_id}-{i}",
                    name=f"car-{clerk_user_id}-{i}",
                    car_class="gt3",
                    master_car_id=uuid6.uuid7(),
                )
            )
        db.add(
            SessionRow(
                id=session_a,
                driver_id=driver_id,
                manifest={"track_id": str(track_a), "car_id": str(car_a)},
                created_at=datetime(2026, 4, 25, tzinfo=UTC),
                status="ready",
            )
        )
        db.add(
            SessionRow(
                id=session_b,
                driver_id=driver_id,
                manifest={"track_id": str(track_b), "car_id": str(car_b)},
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


def _payload(session_ids: list[Any]) -> dict[str, Any]:
    return {
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


def _post(
    client: TestClient, ca: dict[str, Any], who: str, body: dict[str, Any]
) -> Any:
    return client.post(
        "/api/v1/bridge/links",
        headers={"Authorization": f"Bearer {_bearer(ca['keys'], who)}"},
        json=body,
    )


# -- behavior ------------------------------------------------------


def test_second_post_with_same_pair_returns_409(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver_with_bridgeable_pair(
            configured_app["factory"], clerk_user_id="u_d"
        )
    )
    client = TestClient(app)
    first = _post(
        client, configured_app, "u_d", _payload(seeded["session_pair"])
    )
    assert first.status_code == 201
    second = _post(
        client, configured_app, "u_d", _payload(seeded["session_pair"])
    )
    assert second.status_code == 409
    assert second.json()["detail"] == "duplicate_link"


def test_second_post_with_pair_reversed_returns_409(
    configured_app: dict[str, Any],
) -> None:
    """Order-independent dedup: posting (B, A) when (A, B) exists
    is a duplicate. The pair is conceptually a set."""
    seeded = asyncio.run(
        _seed_driver_with_bridgeable_pair(
            configured_app["factory"], clerk_user_id="u_r"
        )
    )
    client = TestClient(app)
    first = _post(
        client, configured_app, "u_r", _payload(seeded["session_pair"])
    )
    assert first.status_code == 201
    reversed_pair = list(reversed(seeded["session_pair"]))
    second = _post(
        client, configured_app, "u_r", _payload(reversed_pair)
    )
    assert second.status_code == 409


def test_dedup_does_not_create_a_second_row(
    configured_app: dict[str, Any],
) -> None:
    """The 409 must not have inserted a duplicate row — the table
    should still hold exactly one link for this pair."""
    seeded = asyncio.run(
        _seed_driver_with_bridgeable_pair(
            configured_app["factory"], clerk_user_id="u_n"
        )
    )
    client = TestClient(app)
    _post(client, configured_app, "u_n", _payload(seeded["session_pair"]))
    _post(client, configured_app, "u_n", _payload(seeded["session_pair"]))

    list_resp = client.get(
        f"/api/v1/driver/{seeded['driver_id']}/bridge/links",
        headers={
            "Authorization": f"Bearer {_bearer(configured_app['keys'], 'u_n')}"
        },
    )
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 1
