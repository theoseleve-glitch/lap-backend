import base64
import json
import time
from collections.abc import AsyncGenerator, Generator
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

    app.dependency_overrides[get_db] = fake_get_db
    app.dependency_overrides[get_jwks_client] = fake_jwks_client
    try:
        yield {"keys": keys}
    finally:
        app.dependency_overrides.clear()


def _bearer(keys: dict[str, Any], sub: str, email: str) -> str:
    now = int(time.time())
    return _sign(
        keys["priv_pem"],
        {"sub": sub, "email": email, "exp": now + 3600},
        keys["kid"],
    )


def _valid_manifest_json() -> str:
    """Smallest manifest that validates against SessionManifest. Used by
    T096 smoke tests that need a valid body now that T097 enforces parsing."""
    return json.dumps(
        {
            "session_id": str(uuid6.uuid7()),
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
    )


def test_ingest_multipart_returns_202(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    bearer = _bearer(keys, "user_ingest_a", "ia@test")
    client = TestClient(app)
    resp = client.post(
        "/api/v1/sessions/ingest",
        headers={
            "Authorization": f"Bearer {bearer}",
            "Idempotency-Key": str(uuid6.uuid7()),
        },
        files={"chunk": ("chunk_0.bin", b"\x00" * 16, "application/octet-stream")},
        data={"manifest": _valid_manifest_json()},
    )
    assert resp.status_code == 202


def test_ingest_returns_status_accepted_in_body(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    bearer = _bearer(keys, "user_ingest_b", "ib@test")
    client = TestClient(app)
    resp = client.post(
        "/api/v1/sessions/ingest",
        headers={
            "Authorization": f"Bearer {bearer}",
            "Idempotency-Key": str(uuid6.uuid7()),
        },
        files={"chunk": ("c.bin", b"\x00", "application/octet-stream")},
        data={"manifest": _valid_manifest_json()},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "accepted"


@pytest.mark.usefixtures("configured_app")
def test_ingest_without_auth_returns_401() -> None:
    client = TestClient(app)
    resp = client.post(
        "/api/v1/sessions/ingest",
        files={"chunk": ("c.bin", b"\x00", "application/octet-stream")},
        data={"manifest": _valid_manifest_json()},
    )
    assert resp.status_code == 401


def test_ingest_accepts_pure_multipart_without_chunk_files(
    configured_app: dict[str, Any],
) -> None:
    # T096 acceptance is "accepts multipart" — files are optional in
    # the multipart body. T097 enforces manifest validity; T098
    # enforces Idempotency-Key. With both supplied but no chunk
    # files, the response is the normal 202.
    keys = configured_app["keys"]
    bearer = _bearer(keys, "user_ingest_c", "ic@test")
    client = TestClient(app)
    resp = client.post(
        "/api/v1/sessions/ingest",
        headers={
            "Authorization": f"Bearer {bearer}",
            "Idempotency-Key": str(uuid6.uuid7()),
        },
        data={"manifest": _valid_manifest_json()},
    )
    assert resp.status_code == 202
