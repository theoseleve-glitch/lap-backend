"""T101 — sha256 per-chunk verification on /sessions/ingest.

The manifest's `telemetry_checksums: dict[str, str]` is keyed by chunk
filename (e.g. "samples.bin") and carries the expected digest as
"sha256:<hex>". When a chunk's filename appears in the manifest, the
computed digest of the streamed bytes must match exactly. A mismatch
is a 422 — the upload is malformed at the data layer.

Chunks whose filename is NOT in the manifest pass through unchecked.
That's an explicit choice: T101 only verifies the relationship asserted
by the manifest. Strict completeness ("every manifest checksum must
have a matching chunk") is a stronger claim deferred to T102+ where
artifact rows are created.
"""

import base64
import hashlib
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
        {"sub": "u_t101", "email": "t101@t", "exp": now + 3600},
        keys["kid"],
    )


def _manifest(session_id: str, checksums: dict[str, str]) -> dict[str, Any]:
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
        "telemetry_checksums": checksums,
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


def _sha256(b: bytes) -> str:
    return f"sha256:{hashlib.sha256(b).hexdigest()}"


def test_chunk_with_matching_checksum_succeeds(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    storage_root: Path = configured_app["storage_root"]
    bearer = _bearer(keys)
    session_id = str(uuid6.uuid7())
    payload = b"samples-payload-XYZ"
    client = TestClient(app)
    resp = client.post(
        "/api/v1/sessions/ingest",
        headers=_headers(bearer),
        files=[
            ("chunks", ("samples.bin", payload, "application/octet-stream"))
        ],
        data={
            "manifest": json.dumps(
                _manifest(session_id, {"samples.bin": _sha256(payload)})
            )
        },
    )
    assert resp.status_code == 202
    assert (storage_root / session_id / "samples.bin").read_bytes() == payload


def test_chunk_with_mismatched_checksum_returns_422(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    bearer = _bearer(keys)
    session_id = str(uuid6.uuid7())
    payload = b"the-actual-bytes"
    wrong_digest = "sha256:" + ("0" * 64)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/sessions/ingest",
        headers=_headers(bearer),
        files=[
            ("chunks", ("samples.bin", payload, "application/octet-stream"))
        ],
        data={
            "manifest": json.dumps(
                _manifest(session_id, {"samples.bin": wrong_digest})
            )
        },
    )
    assert resp.status_code == 422


def test_mismatched_checksum_does_not_persist_chunk(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    storage_root: Path = configured_app["storage_root"]
    bearer = _bearer(keys)
    session_id = str(uuid6.uuid7())
    payload = b"the-actual-bytes"
    wrong_digest = "sha256:" + ("0" * 64)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/sessions/ingest",
        headers=_headers(bearer),
        files=[
            ("chunks", ("samples.bin", payload, "application/octet-stream"))
        ],
        data={
            "manifest": json.dumps(
                _manifest(session_id, {"samples.bin": wrong_digest})
            )
        },
    )
    assert resp.status_code == 422
    # Should not have been persisted — a corrupt upload poisons nothing.
    assert not (storage_root / session_id / "samples.bin").exists()


def test_chunk_filename_not_in_manifest_passes_through(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    storage_root: Path = configured_app["storage_root"]
    bearer = _bearer(keys)
    session_id = str(uuid6.uuid7())
    payload = b"unverified"
    client = TestClient(app)
    # manifest references "samples.bin", chunk uploaded is "extra.bin"
    resp = client.post(
        "/api/v1/sessions/ingest",
        headers=_headers(bearer),
        files=[("chunks", ("extra.bin", payload, "application/octet-stream"))],
        data={
            "manifest": json.dumps(
                _manifest(session_id, {"samples.bin": "sha256:abc"})
            )
        },
    )
    assert resp.status_code == 202
    assert (storage_root / session_id / "extra.bin").read_bytes() == payload


def test_multiple_chunks_each_validated_independently(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    bearer = _bearer(keys)
    session_id = str(uuid6.uuid7())
    a = b"alpha-bytes"
    b = b"bravo-bytes"
    client = TestClient(app)
    resp = client.post(
        "/api/v1/sessions/ingest",
        headers=_headers(bearer),
        files=[
            ("chunks", ("a.bin", a, "application/octet-stream")),
            ("chunks", ("b.bin", b, "application/octet-stream")),
        ],
        data={
            "manifest": json.dumps(
                _manifest(
                    session_id,
                    {"a.bin": _sha256(a), "b.bin": _sha256(b)},
                )
            )
        },
    )
    assert resp.status_code == 202


def test_one_mismatched_among_many_rejects_whole_request(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    bearer = _bearer(keys)
    session_id = str(uuid6.uuid7())
    a = b"alpha-bytes"
    b = b"bravo-bytes"
    client = TestClient(app)
    resp = client.post(
        "/api/v1/sessions/ingest",
        headers=_headers(bearer),
        files=[
            ("chunks", ("a.bin", a, "application/octet-stream")),
            ("chunks", ("b.bin", b, "application/octet-stream")),
        ],
        data={
            "manifest": json.dumps(
                _manifest(
                    session_id,
                    {"a.bin": _sha256(a), "b.bin": "sha256:" + ("f" * 64)},
                )
            )
        },
    )
    assert resp.status_code == 422


def test_malformed_checksum_value_returns_422(
    configured_app: dict[str, Any],
) -> None:
    # The manifest already validated, but the digest string itself is
    # not in "sha256:<64-hex>" form — treat as malformed input rather
    # than crash internally.
    keys = configured_app["keys"]
    bearer = _bearer(keys)
    session_id = str(uuid6.uuid7())
    payload = b"x"
    client = TestClient(app)
    resp = client.post(
        "/api/v1/sessions/ingest",
        headers=_headers(bearer),
        files=[("chunks", ("k.bin", payload, "application/octet-stream"))],
        data={
            "manifest": json.dumps(
                _manifest(session_id, {"k.bin": "not-a-real-digest"})
            )
        },
    )
    assert resp.status_code == 422
