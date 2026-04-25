"""T114 — Last-Event-ID lets SSE clients resume from a cursor.

When the browser reconnects an SSE stream after a network blip it
sends `Last-Event-ID: <n>` in the request headers. The server must
respond with the events strictly after that id — not the whole
buffered history. The bus assigns monotonic per-job event ids; the
SSE endpoint emits each as `id: <n>\\n` before the `event:` line.
"""

import base64
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
        {"sub": "u_t114", "email": "t114@t", "exp": now + 3600},
        keys["kid"],
    )


def _events_in_order(text: str) -> list[str]:
    return [
        line.removeprefix("event: ").strip()
        for line in text.splitlines()
        if line.startswith("event: ")
    ]


def _ids_in_order(text: str) -> list[str]:
    return [
        line.removeprefix("id: ").strip()
        for line in text.splitlines()
        if line.startswith("id: ")
    ]


def test_each_event_carries_an_id_line(
    configured_app: dict[str, Any],
) -> None:
    bus: InMemoryJobEventBus = configured_app["bus"]
    bearer = _bearer(configured_app["keys"])
    job_id = str(uuid6.uuid7())
    bus.publish(job_id, "queued", {})
    bus.publish(job_id, "completed", {})

    client = TestClient(app)
    resp = client.get(
        f"/api/v1/jobs/{job_id}/events",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    ids = _ids_in_order(resp.text)
    # hello (0), queued (1), completed (2) — monotonic, dense.
    assert ids == ["0", "1", "2"]


def test_last_event_id_resumes_from_next_event(
    configured_app: dict[str, Any],
) -> None:
    bus: InMemoryJobEventBus = configured_app["bus"]
    bearer = _bearer(configured_app["keys"])
    job_id = str(uuid6.uuid7())
    bus.publish(job_id, "queued", {})       # id=1 (hello is id=0)
    bus.publish(job_id, "processing", {})   # id=2
    bus.publish(job_id, "stage", {"n": 1})  # id=3
    bus.publish(job_id, "completed", {})    # id=4

    client = TestClient(app)
    # Reconnect after seeing id=2 ("processing"). Resume should
    # contain only id=3 onward and end at completed.
    resp = client.get(
        f"/api/v1/jobs/{job_id}/events",
        headers={
            "Authorization": f"Bearer {bearer}",
            "Last-Event-ID": "2",
        },
    )
    events = _events_in_order(resp.text)
    # No initial hello on resume — the client already has it.
    assert events == ["stage", "completed"]


def test_last_event_id_zero_replays_full_stream(
    configured_app: dict[str, Any],
) -> None:
    # Last-Event-ID=0 means the client only got the initial hello;
    # the server should replay everything from event id 1 onward.
    bus: InMemoryJobEventBus = configured_app["bus"]
    bearer = _bearer(configured_app["keys"])
    job_id = str(uuid6.uuid7())
    bus.publish(job_id, "queued", {})
    bus.publish(job_id, "completed", {})

    client = TestClient(app)
    resp = client.get(
        f"/api/v1/jobs/{job_id}/events",
        headers={
            "Authorization": f"Bearer {bearer}",
            "Last-Event-ID": "0",
        },
    )
    events = _events_in_order(resp.text)
    assert events == ["queued", "completed"]


def test_last_event_id_beyond_buffer_yields_no_events(
    configured_app: dict[str, Any],
) -> None:
    # If the client claims to have already seen events past the end
    # of the buffer (e.g. clock skew, log replay), the server emits
    # no replay and just keepalives until disconnect.
    bus: InMemoryJobEventBus = configured_app["bus"]
    bearer = _bearer(configured_app["keys"])
    job_id = str(uuid6.uuid7())
    bus.publish(job_id, "queued", {})
    bus.publish(job_id, "completed", {})

    client = TestClient(app)
    resp = client.get(
        f"/api/v1/jobs/{job_id}/events",
        headers={
            "Authorization": f"Bearer {bearer}",
            "Last-Event-ID": "99",
        },
    )
    assert _events_in_order(resp.text) == []


def test_invalid_last_event_id_falls_back_to_full_replay(
    configured_app: dict[str, Any],
) -> None:
    # If the header is malformed, treat it as missing rather than 400.
    # This matches the EventSource spec which sends whatever was last
    # received — a corrupted client should still recover.
    bus: InMemoryJobEventBus = configured_app["bus"]
    bearer = _bearer(configured_app["keys"])
    job_id = str(uuid6.uuid7())
    bus.publish(job_id, "completed", {})

    client = TestClient(app)
    resp = client.get(
        f"/api/v1/jobs/{job_id}/events",
        headers={
            "Authorization": f"Bearer {bearer}",
            "Last-Event-ID": "not-a-number",
        },
    )
    events = _events_in_order(resp.text)
    assert events == ["hello", "completed"]
