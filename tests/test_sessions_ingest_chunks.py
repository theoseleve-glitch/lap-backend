"""T100 — chunk persistence to the blob store on /sessions/ingest."""

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
)
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.idempotency_cache import IdempotencyCache
from infra.storage import LocalBlobStore

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
        async with factory() as session:
            yield session

    def fake_jwks_client() -> ClerkJwksClient:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=keys["jwks"])

        return ClerkJwksClient(
            "https://test.clerk", transport=httpx.MockTransport(handler)
        )

    cache = IdempotencyCache(fakeredis.FakeRedis(), ttl_seconds=86400)
    storage_root = tmp_path / "blobs"
    blob_store = LocalBlobStore(storage_root)

    app.dependency_overrides[get_db] = fake_get_db
    app.dependency_overrides[get_jwks_client] = fake_jwks_client
    app.dependency_overrides[get_idempotency_cache] = lambda: cache
    app.dependency_overrides[get_blob_store] = lambda: blob_store
    try:
        yield {
            "keys": keys,
            "blob_store": blob_store,
            "storage_root": storage_root,
        }
    finally:
        app.dependency_overrides.clear()


def _bearer(keys: dict[str, Any]) -> str:
    now = int(time.time())
    return _sign(
        keys["priv_pem"],
        {"sub": "u_t100", "email": "t100@t", "exp": now + 3600},
        keys["kid"],
    )


def _manifest(session_id: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "driver_id": str(uuid6.uuid7()),
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
        "telemetry_checksums": {"samples.bin": "sha256:abc"},
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


def test_ingest_persists_single_chunk_under_session_id(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    storage_root: Path = configured_app["storage_root"]
    bearer = _bearer(keys)
    session_id = str(uuid6.uuid7())
    payload = b"chunk-bytes-0"
    client = TestClient(app)
    resp = client.post(
        "/api/v1/sessions/ingest",
        headers=_headers(bearer),
        files=[("chunks", ("chunk_0.bin", payload, "application/octet-stream"))],
        data={"manifest": json.dumps(_manifest(session_id))},
    )
    assert resp.status_code == 202
    persisted = (storage_root / session_id / "chunk_0.bin").read_bytes()
    assert persisted == payload


def test_ingest_persists_multiple_chunks(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    storage_root: Path = configured_app["storage_root"]
    bearer = _bearer(keys)
    session_id = str(uuid6.uuid7())
    client = TestClient(app)
    resp = client.post(
        "/api/v1/sessions/ingest",
        headers=_headers(bearer),
        files=[
            ("chunks", ("chunk_0.bin", b"AAAA", "application/octet-stream")),
            ("chunks", ("chunk_1.bin", b"BBBB", "application/octet-stream")),
            ("chunks", ("chunk_2.bin", b"CCCC", "application/octet-stream")),
        ],
        data={"manifest": json.dumps(_manifest(session_id))},
    )
    assert resp.status_code == 202
    base = storage_root / session_id
    assert (base / "chunk_0.bin").read_bytes() == b"AAAA"
    assert (base / "chunk_1.bin").read_bytes() == b"BBBB"
    assert (base / "chunk_2.bin").read_bytes() == b"CCCC"


def test_ingest_with_no_chunks_still_returns_202(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    storage_root: Path = configured_app["storage_root"]
    bearer = _bearer(keys)
    session_id = str(uuid6.uuid7())
    client = TestClient(app)
    resp = client.post(
        "/api/v1/sessions/ingest",
        headers=_headers(bearer),
        data={"manifest": json.dumps(_manifest(session_id))},
    )
    assert resp.status_code == 202
    # No chunks were sent, so no session directory should exist.
    assert not (storage_root / session_id).exists()


def test_invalid_manifest_does_not_persist_chunks(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    storage_root: Path = configured_app["storage_root"]
    bearer = _bearer(keys)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/sessions/ingest",
        headers=_headers(bearer),
        files=[
            ("chunks", ("chunk_0.bin", b"DATA", "application/octet-stream")),
        ],
        data={"manifest": "{not-valid"},
    )
    assert resp.status_code == 422
    # Storage root may exist (LocalBlobStore creates it eagerly), but no
    # session_id directory should have been written.
    leaves = list(storage_root.rglob("chunk_0.bin"))
    assert leaves == []


def test_chunk_traversal_filename_is_rejected(
    configured_app: dict[str, Any],
) -> None:
    # If a hostile client smuggles "../" into the filename, the blob
    # store must refuse rather than escape the storage root. Endpoint
    # responds 400 — the safest mapping for unsafe input at the boundary.
    keys = configured_app["keys"]
    bearer = _bearer(keys)
    session_id = str(uuid6.uuid7())
    client = TestClient(app)
    resp = client.post(
        "/api/v1/sessions/ingest",
        headers=_headers(bearer),
        files=[
            ("chunks", ("../escape.bin", b"x", "application/octet-stream")),
        ],
        data={"manifest": json.dumps(_manifest(session_id))},
    )
    assert resp.status_code == 400
