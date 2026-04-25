"""T112 — SSE endpoint GET /api/v1/jobs/{job_id}/events.

The acceptance is just "stream stays alive": the endpoint accepts a
job id, returns text/event-stream, and emits at least one event
plus periodic keepalive comments so intermediaries don't close the
connection. T113 will layer real lifecycle events on top.
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
from app.deps import get_db, get_idempotency_cache, get_jwks_client
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.idempotency_cache import IdempotencyCache

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
    # Bound the SSE stream so tests don't hang waiting for an
    # infinite generator. 50 ms interval + 2 keepalives = ~100 ms
    # to drain the stream, plenty of headroom on slow CI.
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

    app.dependency_overrides[get_db] = fake_get_db
    app.dependency_overrides[get_jwks_client] = fake_jwks_client
    app.dependency_overrides[get_idempotency_cache] = lambda: cache
    try:
        yield {"keys": keys}
    finally:
        app.dependency_overrides.clear()


def _bearer(keys: dict[str, Any]) -> str:
    now = int(time.time())
    return _sign(
        keys["priv_pem"],
        {"sub": "u_t112", "email": "t112@t", "exp": now + 3600},
        keys["kid"],
    )


def test_endpoint_returns_event_stream_content_type(
    configured_app: dict[str, Any],
) -> None:
    bearer = _bearer(configured_app["keys"])
    job_id = str(uuid6.uuid7())
    client = TestClient(app)
    resp = client.get(
        f"/api/v1/jobs/{job_id}/events",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert resp.status_code == 200
    ctype = resp.headers["content-type"]
    # FastAPI/Starlette may append "; charset=utf-8".
    assert ctype.startswith("text/event-stream")


def test_endpoint_emits_initial_hello_event(
    configured_app: dict[str, Any],
) -> None:
    bearer = _bearer(configured_app["keys"])
    job_id = str(uuid6.uuid7())
    client = TestClient(app)
    # Read the full bounded stream and assert the initial event is
    # present. With SSE_MAX_KEEPALIVES=2 the stream closes naturally.
    resp = client.get(
        f"/api/v1/jobs/{job_id}/events",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert resp.status_code == 200
    assert "event: hello" in resp.text
    assert job_id in resp.text


@pytest.mark.usefixtures("configured_app")
def test_endpoint_requires_bearer_auth() -> None:
    job_id = str(uuid6.uuid7())
    client = TestClient(app)
    resp = client.get(f"/api/v1/jobs/{job_id}/events")
    assert resp.status_code == 401


def test_endpoint_emits_keepalive_after_idle(
    configured_app: dict[str, Any],
) -> None:
    # "Stream stays alive": even with no real events to push, the
    # endpoint emits an SSE comment (lines starting ":") periodically
    # so proxies / clients don't time the connection out.
    bearer = _bearer(configured_app["keys"])
    job_id = str(uuid6.uuid7())
    client = TestClient(app)
    resp = client.get(
        f"/api/v1/jobs/{job_id}/events",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert "event: hello" in resp.text
    assert ": keep-alive" in resp.text


def test_endpoint_emits_two_keepalives_when_capped_at_two(
    configured_app: dict[str, Any],
) -> None:
    # Confirms the keepalive cap from env is honored, which both
    # bounds tests and proves the keepalive loop genuinely fires
    # multiple times rather than once.
    bearer = _bearer(configured_app["keys"])
    job_id = str(uuid6.uuid7())
    client = TestClient(app)
    resp = client.get(
        f"/api/v1/jobs/{job_id}/events",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert resp.text.count(": keep-alive") == 2
