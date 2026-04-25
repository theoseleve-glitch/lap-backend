"""T153 — Webhook handler: `customer.subscription.updated`
syncs the Stripe-reported `status` to `user.subscription_status`.

Unlike T152 (which always writes "trialing"), T153 reflects whatever
Stripe says — trial expiry, payment success, plan change, etc.
"""

import hashlib
import hmac
import json
import time
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alembic import command
from app.config import Config as AppConfig
from app.deps import get_config, get_db
from app.main import app
from infra.db import make_engine, make_session_factory
from infra.orm import User

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
WEBHOOK_SECRET = "whsec_test_signing_secret_for_unit_tests"


def _stripe_signature(payload: bytes, secret: str, timestamp: int) -> str:
    signed_payload = f"{timestamp}.".encode() + payload
    sig = hmac.new(
        secret.encode(), signed_payload, hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={sig}"


@pytest.fixture
def configured_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[Any, None, None]:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("REDIS_URL", "redis://x")
    monkeypatch.setenv("CLERK_SECRET", "sk_test_clerk")
    monkeypatch.setenv("STRIPE_SECRET", "sk_test_stripe")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")

    engine = make_engine(url)
    factory = make_session_factory(engine)

    async def fake_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    def fake_config() -> AppConfig:
        return AppConfig()

    app.dependency_overrides[get_db] = fake_get_db
    app.dependency_overrides[get_config] = fake_config
    try:
        yield factory
    finally:
        app.dependency_overrides.clear()


async def _seed_user(
    factory: Any, sub: str, email: str, status: str = "trialing"
) -> User:
    async with factory() as session:
        user = User(
            id=uuid6.uuid7(),
            clerk_user_id=sub,
            email=email,
            subscription_status=status,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


def _subscription_updated_event(user_id: str, status: str) -> bytes:
    body: dict[str, Any] = {
        "id": f"evt_sub_updated_{status}",
        "object": "event",
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_test_updated",
                "object": "subscription",
                "status": status,
                "metadata": {"user_id": user_id},
            }
        },
    }
    return json.dumps(body).encode()


def _post_signed_webhook(payload: bytes) -> Any:
    timestamp = int(time.time())
    sig = _stripe_signature(payload, WEBHOOK_SECRET, timestamp)
    client = TestClient(app)
    return client.post(
        "/api/v1/billing/webhooks",
        content=payload,
        headers={
            "Stripe-Signature": sig,
            "Content-Type": "application/json",
        },
    )


async def test_subscription_updated_syncs_status_active(
    configured_app: Any,
) -> None:
    factory = configured_app
    user = await _seed_user(
        factory, "user_upd_1", "upd1@test", status="trialing"
    )
    payload = _subscription_updated_event(str(user.id), "active")
    resp = _post_signed_webhook(payload)
    assert resp.status_code == 200
    async with factory() as session:
        row = (
            await session.execute(select(User).where(User.id == user.id))
        ).scalar_one()
        assert row.subscription_status == "active"


async def test_subscription_updated_syncs_status_past_due(
    configured_app: Any,
) -> None:
    factory = configured_app
    user = await _seed_user(
        factory, "user_upd_2", "upd2@test", status="active"
    )
    payload = _subscription_updated_event(str(user.id), "past_due")
    resp = _post_signed_webhook(payload)
    assert resp.status_code == 200
    async with factory() as session:
        row = (
            await session.execute(select(User).where(User.id == user.id))
        ).scalar_one()
        assert row.subscription_status == "past_due"


async def test_subscription_updated_syncs_status_canceled(
    configured_app: Any,
) -> None:
    factory = configured_app
    user = await _seed_user(
        factory, "user_upd_3", "upd3@test", status="active"
    )
    payload = _subscription_updated_event(str(user.id), "canceled")
    resp = _post_signed_webhook(payload)
    assert resp.status_code == 200
    async with factory() as session:
        row = (
            await session.execute(select(User).where(User.id == user.id))
        ).scalar_one()
        assert row.subscription_status == "canceled"


async def test_subscription_updated_unknown_user_returns_200(
    configured_app: Any,
) -> None:
    _ = configured_app
    payload = _subscription_updated_event(str(uuid6.uuid7()), "active")
    resp = _post_signed_webhook(payload)
    assert resp.status_code == 200


async def test_subscription_updated_event_without_status_field_is_noop(
    configured_app: Any,
) -> None:
    # Defensive: missing `status` shouldn't crash, just skip the update.
    factory = configured_app
    user = await _seed_user(
        factory, "user_upd_5", "upd5@test", status="active"
    )
    body = {
        "id": "evt_sub_updated_no_status",
        "object": "event",
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_no_status",
                "object": "subscription",
                "metadata": {"user_id": str(user.id)},
            }
        },
    }
    resp = _post_signed_webhook(json.dumps(body).encode())
    assert resp.status_code == 200
    async with factory() as session:
        row = (
            await session.execute(select(User).where(User.id == user.id))
        ).scalar_one()
        assert row.subscription_status == "active"  # unchanged
