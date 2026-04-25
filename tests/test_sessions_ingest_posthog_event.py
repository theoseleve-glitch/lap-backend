"""T170 — `/sessions/ingest` emits a `session_ingested` PostHog event.

The capture is fire-and-forget after the Celery task is dispatched
so analytics never blocks ingestion. We assert via a recording fake
client injected through the `get_posthog_client` dep.

Note: the response status is not asserted here. T104 is BLOCKED on a
cross-repo lap-schemas cascade, which makes the response_model step
fail after dispatch. The capture fires before that step so the
side-effect assertions still hold; the response shape is T104's job.
"""

import asyncio
import base64
import json
import time
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any

import fakeredis
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
from app.deps import (
    get_blob_store,
    get_db,
    get_idempotency_cache,
    get_jwks_client,
    get_posthog_client,
    get_task_dispatcher,
)
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.idempotency_cache import IdempotencyCache
from infra.orm import DriverProfile, User
from infra.storage import LocalBlobStore
from infra.task_dispatcher import InMemoryTaskDispatcher

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


class RecordingPosthog:
    """Fake PostHog client — captures events into an in-memory list."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def capture(self, event: str, **kwargs: Any) -> None:
        self.events.append((event, kwargs))

    def set(self, **kwargs: Any) -> None:  # noqa: ARG002
        return None


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
    db_path = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")

    keys = _build_keypair()
    engine = make_engine(url)
    factory = make_session_factory(engine)
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()

    async def _seed() -> None:
        async with factory() as db:
            db.add(User(id=user_id, clerk_user_id="u_t170", email="t170@t"))
            db.add(
                DriverProfile(
                    id=driver_id, user_id=user_id, display_name="A"
                )
            )
            await db.commit()

    asyncio.run(_seed())

    async def fake_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    def fake_jwks_client() -> ClerkJwksClient:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=keys["jwks"])

        return ClerkJwksClient(
            "https://test.clerk", transport=httpx.MockTransport(handler)
        )

    cache = IdempotencyCache(fakeredis.FakeRedis(), ttl_seconds=86400)
    blob_store = LocalBlobStore(tmp_path / "blobs")
    dispatcher = InMemoryTaskDispatcher()
    posthog = RecordingPosthog()

    app.dependency_overrides[get_db] = fake_get_db
    app.dependency_overrides[get_jwks_client] = fake_jwks_client
    app.dependency_overrides[get_idempotency_cache] = lambda: cache
    app.dependency_overrides[get_blob_store] = lambda: blob_store
    app.dependency_overrides[get_task_dispatcher] = lambda: dispatcher
    app.dependency_overrides[get_posthog_client] = lambda: posthog
    try:
        yield {
            "keys": keys,
            "user_id": user_id,
            "driver_id": driver_id,
            "posthog": posthog,
        }
    finally:
        app.dependency_overrides.clear()


def _bearer(keys: dict[str, Any]) -> str:
    return _sign(
        keys["priv_pem"],
        {"sub": "u_t170", "email": "t170@t", "exp": int(time.time()) + 3600},
        keys["kid"],
    )


def _manifest(session_id: str, driver_id: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "driver_id": driver_id,
        "bundle_version": "v2",
        "capture_app": "lap-coach-logger",
        "capture_app_version": "0.9.0",
        "device_model": "Desktop",
        "device_os": "Windows",
        "device_os_version": "11",
        "source": "iracing",
        "sim_title": "iracing",
        "real_hardware": None,
        "car_id": str(uuid6.uuid7()),
        "track_id": str(uuid6.uuid7()),
        "track_layout": "gp",
        "environment_tag": "sim_practice",
        "objective_tag": "drill",
        "training_block_id": None,
        "started_at": "2026-04-24T10:00:00Z",
        "ended_at": "2026-04-24T10:45:00Z",
        "duration_seconds": 2700.0,
        "telemetry_checksums": {},
        "total_samples": 120000,
        "sample_rate_hz": 60.0,
        "data_quality": {
            "score": 95.0,
            "channel_completeness": 100.0,
            "sample_gaps_count": 0,
            "sample_rate_stability": 0.0,
            "usable_for_overlays": True,
            "usable_for_long_term_coaching": True,
        },
        "sensor_config": {
            "sensors": [{"name": "irsdk", "sample_rate_hz": 60.0}],
        },
    }


def _headers(bearer: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {bearer}",
        "Idempotency-Key": str(uuid6.uuid7()),
    }


def test_successful_ingest_emits_session_ingested_event(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    user_id = configured_app["user_id"]
    driver_id = configured_app["driver_id"]
    posthog: RecordingPosthog = configured_app["posthog"]

    session_id = str(uuid6.uuid7())
    # raise_server_exceptions=False so the T104 cross-repo validation
    # error becomes a 500 response instead of bubbling up — the
    # capture fires before that point regardless.
    client = TestClient(app, raise_server_exceptions=False)
    client.post(
        "/api/v1/sessions/ingest",
        headers=_headers(_bearer(keys)),
        data={"manifest": json.dumps(_manifest(session_id, str(driver_id)))},
    )

    assert len(posthog.events) == 1
    event_name, kwargs = posthog.events[0]
    assert event_name == "session_ingested"
    assert kwargs["distinct_id"] == str(user_id)
    props = kwargs["properties"]
    assert props["session_id"] == session_id
    assert props["source"] == "iracing"
    assert props["environment_tag"] == "sim_practice"


def test_invalid_manifest_does_not_emit_event(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    posthog: RecordingPosthog = configured_app["posthog"]
    client = TestClient(app)
    resp = client.post(
        "/api/v1/sessions/ingest",
        headers=_headers(_bearer(keys)),
        data={"manifest": "{not-valid"},
    )
    assert resp.status_code == 422
    assert posthog.events == []


def test_missing_idempotency_key_does_not_emit_event(
    configured_app: dict[str, Any],
) -> None:
    # 400 fires before dispatch — capture must not happen.
    keys = configured_app["keys"]
    driver_id = configured_app["driver_id"]
    posthog: RecordingPosthog = configured_app["posthog"]
    session_id = str(uuid6.uuid7())
    client = TestClient(app)
    resp = client.post(
        "/api/v1/sessions/ingest",
        headers={"Authorization": f"Bearer {_bearer(keys)}"},
        data={"manifest": json.dumps(_manifest(session_id, str(driver_id)))},
    )
    assert resp.status_code == 400
    assert posthog.events == []
