"""T113 — SSE lifecycle events: queued, processing, stage:N, completed/failed.

The SSE endpoint subscribes to the job event bus and forwards each
event to the client. Terminal events (completed, failed) close the
stream — the client doesn't need keepalives once the job is done.
"""

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
    get_db,
    get_idempotency_cache,
    get_job_event_bus,
    get_jwks_client,
)
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.idempotency_cache import IdempotencyCache
from infra.job_event_bus import InMemoryJobEventBus

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
    # Short keepalive in case the test never reaches a terminal event.
    monkeypatch.setenv("SSE_KEEPALIVE_SECONDS", "0.05")
    monkeypatch.setenv("SSE_MAX_KEEPALIVES", "2")

    db_path = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")

    keys = _build_keypair()
    engine = make_engine(url)
    factory = make_session_factory(engine)

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
    bus = InMemoryJobEventBus()

    app.dependency_overrides[get_db] = fake_get_db
    app.dependency_overrides[get_jwks_client] = fake_jwks_client
    app.dependency_overrides[get_idempotency_cache] = lambda: cache
    app.dependency_overrides[get_job_event_bus] = lambda: bus
    try:
        yield {"keys": keys, "bus": bus}
    finally:
        app.dependency_overrides.clear()


def _bearer(keys: dict[str, Any]) -> str:
    now = int(time.time())
    return _sign(
        keys["priv_pem"],
        {"sub": "u_t113", "email": "t113@t", "exp": now + 3600},
        keys["kid"],
    )


def _events_in_order(text: str) -> list[str]:
    """Extract event names from an SSE response in arrival order."""
    return [
        line.removeprefix("event: ").strip()
        for line in text.splitlines()
        if line.startswith("event: ")
    ]


def test_full_lifecycle_events_arrive_in_order(
    configured_app: dict[str, Any],
) -> None:
    bus: InMemoryJobEventBus = configured_app["bus"]
    bearer = _bearer(configured_app["keys"])
    job_id = str(uuid6.uuid7())

    # Worker simulator: publish the entire lifecycle BEFORE the SSE
    # subscriber connects. The bus replays buffered events in order.
    bus.publish(job_id, "queued", {})
    bus.publish(job_id, "processing", {})
    bus.publish(job_id, "stage", {"n": 1})
    bus.publish(job_id, "stage", {"n": 2})
    bus.publish(job_id, "completed", {"result": "ok"})

    client = TestClient(app)
    resp = client.get(
        f"/api/v1/jobs/{job_id}/events",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert resp.status_code == 200
    events = _events_in_order(resp.text)
    # Initial hello first, then lifecycle events, then completed
    # closes the stream.
    assert events == [
        "hello",
        "queued",
        "processing",
        "stage",
        "stage",
        "completed",
    ]


def test_failed_event_also_closes_stream(
    configured_app: dict[str, Any],
) -> None:
    bus: InMemoryJobEventBus = configured_app["bus"]
    bearer = _bearer(configured_app["keys"])
    job_id = str(uuid6.uuid7())

    bus.publish(job_id, "queued", {})
    bus.publish(job_id, "processing", {})
    bus.publish(job_id, "failed", {"reason": "boom"})

    client = TestClient(app)
    resp = client.get(
        f"/api/v1/jobs/{job_id}/events",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    events = _events_in_order(resp.text)
    assert events[-1] == "failed"


def test_event_data_payload_round_trips_as_json(
    configured_app: dict[str, Any],
) -> None:
    bus: InMemoryJobEventBus = configured_app["bus"]
    bearer = _bearer(configured_app["keys"])
    job_id = str(uuid6.uuid7())

    bus.publish(job_id, "stage", {"n": 3, "label": "normalize"})
    bus.publish(job_id, "completed", {})

    client = TestClient(app)
    resp = client.get(
        f"/api/v1/jobs/{job_id}/events",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    # Find the data line for the stage event and parse it.
    text = resp.text
    stage_idx = text.index("event: stage")
    data_line = text[stage_idx:].split("\n")[1]
    payload = json.loads(data_line.removeprefix("data: "))
    assert payload == {"n": 3, "label": "normalize"}


def test_events_for_other_jobs_are_not_leaked(
    configured_app: dict[str, Any],
) -> None:
    bus: InMemoryJobEventBus = configured_app["bus"]
    bearer = _bearer(configured_app["keys"])
    my_job = str(uuid6.uuid7())
    other_job = str(uuid6.uuid7())

    bus.publish(other_job, "queued", {})
    bus.publish(other_job, "completed", {})
    bus.publish(my_job, "queued", {})
    bus.publish(my_job, "completed", {})

    client = TestClient(app)
    resp = client.get(
        f"/api/v1/jobs/{my_job}/events",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    # Only my job's events plus the initial hello — no cross-talk.
    events = _events_in_order(resp.text)
    assert events == ["hello", "queued", "completed"]
