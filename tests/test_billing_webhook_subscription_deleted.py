"""T154 — Webhook handler: `customer.subscription.deleted`
sets `user.subscription_status = "canceled"`.

`subscription.deleted` is Stripe's "fully gone" event (distinct
from `subscription.updated` with status=canceled, which can fire
during pause/resume cycles). The user's access is gated by reading
this status; the gating dep itself lands in T162.
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
    factory: Any, sub: str, email: str, status: str = "active"
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


def _subscription_deleted_event(user_id: str) -> bytes:
    body: dict[str, Any] = {
        "id": "evt_sub_deleted_1",
        "object": "event",
        "type": "customer.subscription.deleted",
        "data": {
            "object": {
                "id": "sub_test_deleted",
                "object": "subscription",
                # Stripe sends status="canceled" on deleted events,
                # but our handler hardcodes "canceled" anyway since
                # `deleted` is the canonical end state.
                "status": "canceled",
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


async def test_subscription_deleted_sets_user_canceled(
    configured_app: Any,
) -> None:
    factory = configured_app
    user = await _seed_user(factory, "user_del_1", "del1@test", status="active")
    payload = _subscription_deleted_event(str(user.id))
    resp = _post_signed_webhook(payload)
    assert resp.status_code == 200
    async with factory() as session:
        row = (
            await session.execute(select(User).where(User.id == user.id))
        ).scalar_one()
        assert row.subscription_status == "canceled"


async def test_subscription_deleted_from_trialing_also_cancels(
    configured_app: Any,
) -> None:
    # Trial ended without a payment method on file → Stripe sends
    # subscription.deleted directly. Should still flip to canceled.
    factory = configured_app
    user = await _seed_user(
        factory, "user_del_2", "del2@test", status="trialing"
    )
    payload = _subscription_deleted_event(str(user.id))
    resp = _post_signed_webhook(payload)
    assert resp.status_code == 200
    async with factory() as session:
        row = (
            await session.execute(select(User).where(User.id == user.id))
        ).scalar_one()
        assert row.subscription_status == "canceled"


async def test_subscription_deleted_unknown_user_returns_200(
    configured_app: Any,
) -> None:
    _ = configured_app
    payload = _subscription_deleted_event(str(uuid6.uuid7()))
    resp = _post_signed_webhook(payload)
    assert resp.status_code == 200


async def test_subscription_deleted_other_users_untouched(
    configured_app: Any,
) -> None:
    factory = configured_app
    target = await _seed_user(
        factory, "user_del_target", "deltgt@test", status="active"
    )
    bystander = await _seed_user(
        factory, "user_del_bystander", "delby@test", status="active"
    )
    payload = _subscription_deleted_event(str(target.id))
    resp = _post_signed_webhook(payload)
    assert resp.status_code == 200
    async with factory() as session:
        bystander_row = (
            await session.execute(
                select(User).where(User.id == bystander.id)
            )
        ).scalar_one()
        assert bystander_row.subscription_status == "active"
