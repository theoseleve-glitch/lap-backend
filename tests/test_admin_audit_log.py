"""T197 (sprint2-backend) — admin audit log.

Mutating admin actions (billing sync, refund) write an
append-only row to `admin_audit_log` carrying:
  - admin_user_id: the admin caller's User.id
  - action: a short stable string ("billing.sync",
    "billing.refund") that downstream tooling pivots on
  - target_id: the subject of the action (target user for
    sync, charge id for refund)
  - metadata_json: structured per-action context (e.g. the
    Stripe refund id + amount) for forensic detail
  - created_at: server-default timestamp (UTC)

Pinned behaviors (each test = one regulator-defensible
property):
  - the helper writes a row matching the input shape
  - billing.sync writes a row when admin hits the endpoint
  - billing.refund writes a row when admin hits the endpoint
  - failed admin actions DO NOT swallow the audit (the
    relevant row still gets written before any propagating
    error — though we test the success path here; the
    failure-path semantics are a future task)

Why a separate test file rather than folding into the
existing per-endpoint tests? The audit log is a cross-
cutting concern; a single regression (e.g. someone removes
the helper call from one endpoint) is easier to spot in a
file dedicated to "did we audit this?"
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import jwt
import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from app.config import Config as AppConfig
from app.deps import get_config, get_db, get_jwks_client
from app.main import app
from infra.admin_audit import record_admin_action
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import AdminAuditLog, User

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
        "kty": "RSA", "kid": kid, "use": "sig", "alg": "RS256",
        "n": _b64u(n), "e": _b64u(e),
    }
    return {"priv_pem": priv_pem, "kid": kid, "jwks": {"keys": [jwk]}}


def _bearer(keys: dict[str, Any], sub: str, email: str) -> str:
    return jwt.encode(
        {"sub": sub, "email": email, "exp": int(time.time()) + 3600},
        keys["priv_pem"],
        algorithm="RS256",
        headers={"kid": keys["kid"]},
    )


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

    def fake_config() -> AppConfig:
        return AppConfig()

    app.dependency_overrides[get_db] = fake_get_db
    app.dependency_overrides[get_jwks_client] = fake_jwks_client
    app.dependency_overrides[get_config] = fake_config
    try:
        yield {"keys": keys, "factory": factory}
    finally:
        app.dependency_overrides.clear()


async def _seed_user(
    factory: async_sessionmaker[AsyncSession],
    *,
    sub: str,
    email: str,
    is_admin: bool = False,
    stripe_customer_id: str | None = None,
) -> uuid6.UUID:
    async with factory() as db:
        user = User(
            id=uuid6.uuid7(),
            clerk_user_id=sub,
            email=email,
            is_admin=is_admin,
            stripe_customer_id=stripe_customer_id,
        )
        db.add(user)
        await db.commit()
        return user.id


async def _audit_rows(
    factory: async_sessionmaker[AsyncSession],
) -> list[AdminAuditLog]:
    async with factory() as db:
        return list(
            (await db.execute(select(AdminAuditLog))).scalars().all()
        )


# -- helper unit tests -------------------------------------------


def test_record_admin_action_persists_required_fields(
    configured_app: dict[str, Any],
) -> None:
    """T197 #1 — the helper is the only writer; if it skips a
    required field every endpoint downstream will too. Pin
    that admin_user_id, action, target_id, metadata_json, and
    created_at all land on the row when the helper is called
    with all fields populated."""
    factory = configured_app["factory"]
    admin_id = asyncio.run(
        _seed_user(
            factory, sub="t197_helper_admin", email="h@t.io",
            is_admin=True,
        )
    )
    target = uuid6.uuid7()

    async def go() -> None:
        async with factory() as db:
            await record_admin_action(
                db,
                admin_user_id=admin_id,
                action="billing.refund",
                target_id=target,
                metadata={"refund_id": "re_x", "amount": 1500},
            )
            await db.commit()  # helper doesn't commit; caller does.

    asyncio.run(go())

    rows = asyncio.run(_audit_rows(factory))
    assert len(rows) == 1
    row = rows[0]
    assert row.admin_user_id == admin_id
    assert row.action == "billing.refund"
    assert row.target_id == target
    assert row.metadata_json == {"refund_id": "re_x", "amount": 1500}
    assert row.created_at is not None, (
        "created_at must be auto-populated by server_default — "
        "missing it makes the audit log unforensic"
    )


def test_record_admin_action_metadata_optional(
    configured_app: dict[str, Any],
) -> None:
    """Some admin actions carry no structured context (just the
    fact-of). The helper must accept None for metadata and
    write a row with metadata_json=NULL — not a JSON-encoded
    null string."""
    factory = configured_app["factory"]
    admin_id = asyncio.run(
        _seed_user(
            factory, sub="t197_meta_none_admin", email="n@t.io",
            is_admin=True,
        )
    )

    async def go() -> None:
        async with factory() as db:
            await record_admin_action(
                db,
                admin_user_id=admin_id,
                action="some.action",
                target_id=None,
                metadata=None,
            )
            await db.commit()

    asyncio.run(go())
    rows = asyncio.run(_audit_rows(factory))
    assert len(rows) == 1
    assert rows[0].metadata_json is None
    assert rows[0].target_id is None


# -- integration: billing.sync writes audit ----------------------


def test_admin_billing_sync_writes_audit_row(
    configured_app: dict[str, Any],
) -> None:
    """The endpoint already has a structlog `info` line — that's
    operator visibility, not auditability (logs are ephemeral
    and not queryable as a table). The audit row is the durable
    artifact regulators (and your future legal team) will ask
    for."""
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    admin_id = asyncio.run(
        _seed_user(
            factory,
            sub="t197_sync_admin",
            email="syncadm@t.io",
            is_admin=True,
        )
    )
    target_id = asyncio.run(
        _seed_user(
            factory,
            sub="t197_sync_target",
            email="synctgt@t.io",
            stripe_customer_id="cus_audit_sync",
        )
    )
    token = _bearer(keys, "t197_sync_admin", "syncadm@t.io")

    fake_listing = type("L", (), {})()
    fake_listing.auto_paging_iter = lambda: iter([{"status": "active"}])
    with patch("stripe.Subscription.list", return_value=fake_listing):
        client = TestClient(app)
        resp = client.post(
            f"/api/v1/admin/billing/sync/{target_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text

    rows = asyncio.run(_audit_rows(factory))
    matching = [r for r in rows if r.action == "billing.sync"]
    assert len(matching) == 1, (
        "expected exactly one billing.sync audit row; "
        f"got rows: {[(r.action, r.admin_user_id) for r in rows]}"
    )
    row = matching[0]
    assert row.admin_user_id == admin_id, (
        "audit row must point at the ADMIN who performed the "
        "action, not the target user — wrong user_id makes the "
        "audit log uninvestigable"
    )
    assert row.target_id == target_id, (
        "target_id must point at the affected user (the sync "
        "subject) so the audit log answers "
        "'who did what to whom'"
    )


def test_admin_billing_refund_writes_audit_row(
    configured_app: dict[str, Any],
) -> None:
    """Refund is a money-moving action — the audit row is the
    record of which admin issued which refund, with the Stripe
    refund_id pinned in metadata so a regulator can cross-
    reference Stripe's ledger."""
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    admin_id = asyncio.run(
        _seed_user(
            factory,
            sub="t197_refund_admin",
            email="refadm@t.io",
            is_admin=True,
        )
    )
    token = _bearer(keys, "t197_refund_admin", "refadm@t.io")

    fake_refund = {
        "id": "re_t197_audit",
        "status": "succeeded",
        "amount": 2500,
        "currency": "eur",
    }
    with patch("stripe.Refund.create", return_value=fake_refund):
        client = TestClient(app)
        resp = client.post(
            "/api/v1/admin/billing/refund",
            headers={"Authorization": f"Bearer {token}"},
            json={"charge_id": "ch_t197_audit", "amount": 2500},
        )
    assert resp.status_code == 200

    rows = asyncio.run(_audit_rows(factory))
    matching = [r for r in rows if r.action == "billing.refund"]
    assert len(matching) == 1
    row = matching[0]
    assert row.admin_user_id == admin_id
    assert row.metadata_json is not None
    assert row.metadata_json.get("refund_id") == "re_t197_audit", (
        "Stripe refund_id must be pinned in metadata so the "
        "audit log is cross-referenceable to Stripe's ledger"
    )
    assert row.metadata_json.get("amount") == 2500


# -- isolation: non-admin paths don't write audit ---------------


def test_non_admin_billing_refund_does_not_write_audit(
    configured_app: dict[str, Any],
) -> None:
    """A non-admin caller hitting the refund endpoint is rejected
    at the auth layer (403) before the handler runs. No audit
    row should be created — the action never happened. A
    regression that writes an audit row before the auth check
    would pollute the log with attempted-but-blocked actions."""
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    asyncio.run(
        _seed_user(
            factory,
            sub="t197_nonadmin",
            email="nonadm@t.io",
            is_admin=False,
        )
    )
    token = _bearer(keys, "t197_nonadmin", "nonadm@t.io")

    with patch("stripe.Refund.create"):
        client = TestClient(app)
        resp = client.post(
            "/api/v1/admin/billing/refund",
            headers={"Authorization": f"Bearer {token}"},
            json={"charge_id": "ch_blocked"},
        )
    assert resp.status_code == 403

    rows = asyncio.run(_audit_rows(factory))
    assert rows == [], (
        "audit log must NOT record blocked attempts — those "
        "belong in security/access logs, not the action ledger"
    )
