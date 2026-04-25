import base64
import hashlib
import hmac
import json
import time
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from alembic import command
from app.deps import get_db
from app.main import app
from infra.db import make_engine, make_session_factory

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

# 32-byte key, base64-encoded → "whsec_<base64>" form
_RAW_KEY = b"\x01" * 32
_SECRET = "whsec_" + base64.b64encode(_RAW_KEY).decode()


def _sign(svix_id: str, ts: str, body: bytes) -> str:
    payload = f"{svix_id}.{ts}.".encode() + body
    digest = hmac.new(_RAW_KEY, payload, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


@pytest.fixture
def configured_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("CLERK_SECRET", "sk_test_clerk")
    monkeypatch.setenv("STRIPE_SECRET", "sk_test_stripe")
    monkeypatch.setenv("CLERK_WEBHOOK_SECRET", _SECRET)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")

    engine = make_engine(url)
    factory = make_session_factory(engine)

    async def fake_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    from app.deps import get_config

    get_config.cache_clear()
    app.dependency_overrides[get_db] = fake_get_db
    try:
        yield None
    finally:
        app.dependency_overrides.clear()
        get_config.cache_clear()


def _payload() -> dict[str, Any]:
    return {
        "object": "event",
        "type": "user.created",
        "data": {
            "id": "user_clerk_signed",
            "primary_email_address_id": "idn_1",
            "email_addresses": [
                {"id": "idn_1", "email_address": "signed@test"}
            ],
        },
    }


@pytest.mark.usefixtures("configured_app")
def test_valid_signature_is_accepted() -> None:
    client = TestClient(app)
    body = json.dumps(_payload()).encode()
    svix_id = "msg_2zzz"
    ts = str(int(time.time()))
    sig = _sign(svix_id, ts, body)
    resp = client.post(
        "/api/v1/auth/webhooks",
        content=body,
        headers={
            "content-type": "application/json",
            "svix-id": svix_id,
            "svix-timestamp": ts,
            "svix-signature": sig,
        },
    )
    assert resp.status_code == 200


@pytest.mark.usefixtures("configured_app")
def test_invalid_signature_is_rejected() -> None:
    client = TestClient(app)
    body = json.dumps(_payload()).encode()
    svix_id = "msg_2zzz"
    ts = str(int(time.time()))
    bad_sig = "v1," + base64.b64encode(b"\x00" * 32).decode()
    resp = client.post(
        "/api/v1/auth/webhooks",
        content=body,
        headers={
            "content-type": "application/json",
            "svix-id": svix_id,
            "svix-timestamp": ts,
            "svix-signature": bad_sig,
        },
    )
    assert resp.status_code == 401


@pytest.mark.usefixtures("configured_app")
def test_missing_signature_header_rejected() -> None:
    client = TestClient(app)
    body = json.dumps(_payload()).encode()
    resp = client.post(
        "/api/v1/auth/webhooks",
        content=body,
        headers={
            "content-type": "application/json",
            "svix-id": "msg_x",
            "svix-timestamp": str(int(time.time())),
        },
    )
    assert resp.status_code == 401


@pytest.mark.usefixtures("configured_app")
def test_missing_id_header_rejected() -> None:
    client = TestClient(app)
    body = json.dumps(_payload()).encode()
    ts = str(int(time.time()))
    resp = client.post(
        "/api/v1/auth/webhooks",
        content=body,
        headers={
            "content-type": "application/json",
            "svix-timestamp": ts,
            "svix-signature": _sign("msg_x", ts, body),
        },
    )
    assert resp.status_code == 401


@pytest.mark.usefixtures("configured_app")
def test_tampered_body_rejected() -> None:
    client = TestClient(app)
    original = json.dumps(_payload()).encode()
    svix_id = "msg_t"
    ts = str(int(time.time()))
    sig = _sign(svix_id, ts, original)
    tampered = json.dumps(
        {**_payload(), "type": "user.deleted"}
    ).encode()
    resp = client.post(
        "/api/v1/auth/webhooks",
        content=tampered,
        headers={
            "content-type": "application/json",
            "svix-id": svix_id,
            "svix-timestamp": ts,
            "svix-signature": sig,
        },
    )
    assert resp.status_code == 401


@pytest.mark.usefixtures("configured_app")
def test_old_timestamp_rejected() -> None:
    client = TestClient(app)
    body = json.dumps(_payload()).encode()
    svix_id = "msg_old"
    old_ts = str(int(time.time()) - 60 * 60)  # 1 hour old
    sig = _sign(svix_id, old_ts, body)
    resp = client.post(
        "/api/v1/auth/webhooks",
        content=body,
        headers={
            "content-type": "application/json",
            "svix-id": svix_id,
            "svix-timestamp": old_ts,
            "svix-signature": sig,
        },
    )
    assert resp.status_code == 401


@pytest.mark.usefixtures("configured_app")
def test_multiple_signatures_one_valid_accepted() -> None:
    client = TestClient(app)
    body = json.dumps(_payload()).encode()
    svix_id = "msg_multi"
    ts = str(int(time.time()))
    bad = "v1," + base64.b64encode(b"\x00" * 32).decode()
    good = _sign(svix_id, ts, body)
    resp = client.post(
        "/api/v1/auth/webhooks",
        content=body,
        headers={
            "content-type": "application/json",
            "svix-id": svix_id,
            "svix-timestamp": ts,
            "svix-signature": f"{bad} {good}",
        },
    )
    assert resp.status_code == 200
