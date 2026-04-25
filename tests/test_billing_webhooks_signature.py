"""T151 — `POST /api/v1/billing/webhooks` validates Stripe signature.

Stripe signs every webhook delivery with an HMAC-SHA256 of
``f"{timestamp}.{body}"`` keyed by the endpoint's signing secret.
We verify via `stripe.Webhook.construct_event`. Invalid or absent
signatures must return 400; valid signatures return 200 (handler
logic for specific event types lands in T152+).
"""

import hashlib
import hmac
import json
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient

from alembic import command
from app.config import Config as AppConfig
from app.deps import get_config
from app.main import app

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

WEBHOOK_SECRET = "whsec_test_signing_secret_for_unit_tests"


def _stripe_signature(payload: bytes, secret: str, timestamp: int) -> str:
    """Compute the `Stripe-Signature` header for a payload.
    Format: ``t=<unix>,v1=<hex hmac sha256>``."""
    signed_payload = f"{timestamp}.".encode() + payload
    sig = hmac.new(
        secret.encode(), signed_payload, hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={sig}"


@pytest.fixture
def configured_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("REDIS_URL", "redis://x")
    monkeypatch.setenv("CLERK_SECRET", "sk_test_clerk")
    monkeypatch.setenv("STRIPE_SECRET", "sk_test_stripe")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")

    def fake_config() -> AppConfig:
        return AppConfig()

    app.dependency_overrides[get_config] = fake_config
    try:
        yield None
    finally:
        app.dependency_overrides.clear()


def _event_payload() -> bytes:
    # Mirrors the shape Stripe actually sends — top-level
    # `object: "event"` is the discriminator the SDK introspects.
    body: dict[str, Any] = {
        "id": "evt_test_webhook",
        "object": "event",
        "type": "customer.subscription.created",
        "data": {"object": {"id": "sub_test_1", "object": "subscription"}},
    }
    return json.dumps(body).encode()


def test_webhook_with_valid_signature_returns_200(
    configured_app: None,  # noqa: ARG001
) -> None:
    payload = _event_payload()
    timestamp = int(time.time())
    sig = _stripe_signature(payload, WEBHOOK_SECRET, timestamp)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/billing/webhooks",
        content=payload,
        headers={
            "Stripe-Signature": sig,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200


def test_webhook_with_invalid_signature_returns_400(
    configured_app: None,  # noqa: ARG001
) -> None:
    payload = _event_payload()
    bogus_sig = (
        "t=1,v1=deadbeef000000000000000000000000000000000000000000000000abcd"
    )
    client = TestClient(app)
    resp = client.post(
        "/api/v1/billing/webhooks",
        content=payload,
        headers={
            "Stripe-Signature": bogus_sig,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 400


def test_webhook_with_wrong_secret_returns_400(
    configured_app: None,  # noqa: ARG001
) -> None:
    payload = _event_payload()
    timestamp = int(time.time())
    sig = _stripe_signature(payload, "whsec_wrong_secret", timestamp)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/billing/webhooks",
        content=payload,
        headers={
            "Stripe-Signature": sig,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 400


def test_webhook_without_signature_header_returns_400(
    configured_app: None,  # noqa: ARG001
) -> None:
    client = TestClient(app)
    resp = client.post(
        "/api/v1/billing/webhooks",
        content=_event_payload(),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_webhook_with_tampered_payload_returns_400(
    configured_app: None,  # noqa: ARG001
) -> None:
    # Sign one payload but POST a different one — Stripe's HMAC
    # binds the body to the signature, so this must reject.
    original = _event_payload()
    timestamp = int(time.time())
    sig = _stripe_signature(original, WEBHOOK_SECRET, timestamp)
    tampered = original.replace(b"sub_test_1", b"sub_attacker_1")
    client = TestClient(app)
    resp = client.post(
        "/api/v1/billing/webhooks",
        content=tampered,
        headers={
            "Stripe-Signature": sig,
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 400
