"""T156 — Webhook handler: `invoice.payment_succeeded`
sets `user.subscription_status = "active"`.

Fires monthly on successful billing, and once on trial→paid
transition. Recovers users from `past_due` if a retry succeeds.
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
from infra.notifications import NotificationQueue, get_notification_queue
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
) -> Generator[dict[str, Any], None, None]:
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
    notif_q = NotificationQueue()

    async def fake_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    def fake_config() -> AppConfig:
        return AppConfig()

    def fake_notif_queue() -> NotificationQueue:
        return notif_q

    app.dependency_overrides[get_db] = fake_get_db
    app.dependency_overrides[get_config] = fake_config
    app.dependency_overrides[get_notification_queue] = fake_notif_queue
    try:
        yield {"factory": factory, "notif_q": notif_q}
    finally:
        app.dependency_overrides.clear()


async def _seed_user(
    factory: Any, sub: str, email: str, status: str
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


def _payment_succeeded_event(user_id: str) -> bytes:
    body: dict[str, Any] = {
        "id": "evt_invoice_paid_1",
        "object": "event",
        "type": "invoice.payment_succeeded",
        "data": {
            "object": {
                "id": "in_test_paid_1",
                "object": "invoice",
                "customer": "cus_test_2",
                "subscription": "sub_test_2",
                "amount_paid": 1500,
                "currency": "eur",
                "subscription_details": {
                    "metadata": {"user_id": user_id},
                },
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


async def test_invoice_payment_succeeded_recovers_past_due(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    user = await _seed_user(
        factory, "user_ps_1", "ps1@test", status="past_due"
    )
    payload = _payment_succeeded_event(str(user.id))
    resp = _post_signed_webhook(payload)
    assert resp.status_code == 200
    async with factory() as session:
        row = (
            await session.execute(select(User).where(User.id == user.id))
        ).scalar_one()
        assert row.subscription_status == "active"


async def test_invoice_payment_succeeded_promotes_trialing(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    user = await _seed_user(
        factory, "user_ps_2", "ps2@test", status="trialing"
    )
    payload = _payment_succeeded_event(str(user.id))
    resp = _post_signed_webhook(payload)
    assert resp.status_code == 200
    async with factory() as session:
        row = (
            await session.execute(select(User).where(User.id == user.id))
        ).scalar_one()
        assert row.subscription_status == "active"


async def test_invoice_payment_succeeded_unknown_user_returns_200(
    configured_app: dict[str, Any],
) -> None:
    _ = configured_app
    payload = _payment_succeeded_event(str(uuid6.uuid7()))
    resp = _post_signed_webhook(payload)
    assert resp.status_code == 200


async def test_invoice_payment_succeeded_other_users_untouched(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    target = await _seed_user(
        factory, "user_ps_target", "pstgt@test", status="past_due"
    )
    bystander = await _seed_user(
        factory, "user_ps_by", "psby@test", status="past_due"
    )
    payload = _payment_succeeded_event(str(target.id))
    resp = _post_signed_webhook(payload)
    assert resp.status_code == 200
    async with factory() as session:
        bystander_row = (
            await session.execute(
                select(User).where(User.id == bystander.id)
            )
        ).scalar_one()
        assert bystander_row.subscription_status == "past_due"


async def test_invoice_payment_succeeded_does_not_enqueue_notification(
    configured_app: dict[str, Any],
) -> None:
    # Successful payments are silent — no email/push needed.
    factory = configured_app["factory"]
    notif_q: NotificationQueue = configured_app["notif_q"]
    user = await _seed_user(
        factory, "user_ps_silent", "ps_silent@test", status="past_due"
    )
    payload = _payment_succeeded_event(str(user.id))
    resp = _post_signed_webhook(payload)
    assert resp.status_code == 200
    assert notif_q.drain() == []
