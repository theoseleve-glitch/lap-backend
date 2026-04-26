"""T003 (sprint2-backend) — composable timeline filters.

`GET /api/v1/driver/timeline` accepts:
- `source`        — manifest.source (e.g. "iracing", "racebox")
- `track_id`      — manifest.track_id (UUID)
- `car_id`        — manifest.car_id (UUID)
- `from_date`     — Session.created_at >= ISO8601
- `to_date`       — Session.created_at <  ISO8601

Filters AND together. Empty/missing filters are no-ops. Date
columns use Session.created_at (the indexed column); manifest
fields use SQL-side JSON extraction so the filter happens
DB-side and pagination math stays clean.
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


async def _seed(
    factory: Any,
    *,
    driver_id: Any,
    created_at: datetime,
    source: str = "iracing",
    track_id: Any | None = None,
    car_id: Any | None = None,
) -> Any:
    """Seed Session+SessionRecord with manifest fields the filters
    will key on. Defaults give every row a sensible source/track/car
    so test setup stays minimal."""
    session_id = uuid6.uuid7()
    record_id = uuid6.uuid7()
    track_id = track_id or uuid6.uuid7()
    car_id = car_id or uuid6.uuid7()
    async with factory() as db:
        db.add(
            SessionRow(
                id=session_id,
                driver_id=driver_id,
                manifest={
                    "bundle_version": "v2",
                    "source": source,
                    "track_id": str(track_id),
                    "car_id": str(car_id),
                },
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
                    "best_lap_ms": 90_000,
                    "total_laps": 5,
                    "valid_laps": 5,
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


# -- single filters ------------------------------------------------


def test_filter_by_source_returns_only_matching(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_src", email="s@t")
    )
    base = datetime(2026, 4, 26, 10, 0, tzinfo=UTC)
    iracing = asyncio.run(
        _seed(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            created_at=base,
            source="iracing",
        )
    )
    asyncio.run(
        _seed(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            created_at=base + timedelta(hours=1),
            source="racebox",
        )
    )
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {_bearer(configured_app['keys'], 'u_src')}"}
    resp = client.get(
        "/api/v1/driver/timeline?source=iracing", headers=headers
    )
    assert resp.status_code == 200
    body = resp.json()
    ids = [e["session_id"] for e in body["entries"]]
    assert ids == [str(iracing)]


def test_filter_by_track_id(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_tk", email="t@t")
    )
    track_a = uuid6.uuid7()
    track_b = uuid6.uuid7()
    base = datetime(2026, 4, 26, tzinfo=UTC)
    on_a = asyncio.run(
        _seed(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            created_at=base,
            track_id=track_a,
        )
    )
    asyncio.run(
        _seed(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            created_at=base + timedelta(hours=1),
            track_id=track_b,
        )
    )
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {_bearer(configured_app['keys'], 'u_tk')}"}
    resp = client.get(
        f"/api/v1/driver/timeline?track_id={track_a}", headers=headers
    )
    body = resp.json()
    assert [e["session_id"] for e in body["entries"]] == [str(on_a)]


def test_filter_by_car_id(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_car", email="c@t")
    )
    car_a = uuid6.uuid7()
    car_b = uuid6.uuid7()
    base = datetime(2026, 4, 26, tzinfo=UTC)
    on_a = asyncio.run(
        _seed(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            created_at=base,
            car_id=car_a,
        )
    )
    asyncio.run(
        _seed(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            created_at=base + timedelta(hours=1),
            car_id=car_b,
        )
    )
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {_bearer(configured_app['keys'], 'u_car')}"}
    resp = client.get(
        f"/api/v1/driver/timeline?car_id={car_a}", headers=headers
    )
    body = resp.json()
    assert [e["session_id"] for e in body["entries"]] == [str(on_a)]


def test_filter_by_date_range_inclusive_from_exclusive_to(
    configured_app: dict[str, Any],
) -> None:
    """from_date is inclusive (>=), to_date exclusive (<) — the
    standard half-open interval that lets adjacent ranges tile
    without overlap."""
    seeded = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_dr", email="d@t")
    )
    too_old = asyncio.run(
        _seed(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            created_at=datetime(2026, 4, 23, tzinfo=UTC),
        )
    )
    in_window = asyncio.run(
        _seed(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            created_at=datetime(2026, 4, 25, 12, 0, tzinfo=UTC),
        )
    )
    too_new = asyncio.run(
        _seed(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            created_at=datetime(2026, 4, 28, tzinfo=UTC),
        )
    )
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {_bearer(configured_app['keys'], 'u_dr')}"}
    resp = client.get(
        "/api/v1/driver/timeline"
        "?from_date=2026-04-25T00:00:00%2B00:00"
        "&to_date=2026-04-27T00:00:00%2B00:00",
        headers=headers,
    )
    body = resp.json()
    ids = [e["session_id"] for e in body["entries"]]
    assert str(in_window) in ids
    assert str(too_old) not in ids
    assert str(too_new) not in ids


# -- composability -------------------------------------------------


def test_multiple_filters_and_together(
    configured_app: dict[str, Any],
) -> None:
    """Source AND track AND car must all match — returning the
    intersection, not the union."""
    seeded = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_and", email="a@t")
    )
    track = uuid6.uuid7()
    car = uuid6.uuid7()
    base = datetime(2026, 4, 26, tzinfo=UTC)
    full_match = asyncio.run(
        _seed(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            created_at=base,
            source="iracing",
            track_id=track,
            car_id=car,
        )
    )
    asyncio.run(
        _seed(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            created_at=base + timedelta(hours=1),
            source="iracing",  # same
            track_id=track,  # same
            car_id=uuid6.uuid7(),  # diff
        )
    )
    asyncio.run(
        _seed(
            configured_app["factory"],
            driver_id=seeded["driver_id"],
            created_at=base + timedelta(hours=2),
            source="racebox",  # diff
            track_id=track,
            car_id=car,
        )
    )
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {_bearer(configured_app['keys'], 'u_and')}"}
    resp = client.get(
        "/api/v1/driver/timeline"
        f"?source=iracing&track_id={track}&car_id={car}",
        headers=headers,
    )
    body = resp.json()
    assert [e["session_id"] for e in body["entries"]] == [str(full_match)]


def test_no_filters_returns_all_sessions(
    configured_app: dict[str, Any],
) -> None:
    """Empty filters are a no-op — equivalent to T001's behavior."""
    seeded = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="u_no", email="n@t")
    )
    base = datetime(2026, 4, 26, tzinfo=UTC)
    for i in range(3):
        asyncio.run(
            _seed(
                configured_app["factory"],
                driver_id=seeded["driver_id"],
                created_at=base + timedelta(hours=i),
            )
        )
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {_bearer(configured_app['keys'], 'u_no')}"}
    resp = client.get("/api/v1/driver/timeline", headers=headers)
    assert len(resp.json()["entries"]) == 3
