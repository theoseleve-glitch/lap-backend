"""T168 (sprint2-backend) — seat-count proration on member changes.

When an Organization carries `stripe_subscription_id`, the
endpoints that change member count (T165 accept-invite, T166
remove-member) must keep Stripe in sync. Specifically:

  - on accept-invite, Stripe is modified with the new member
    count (post-accept) and `proration_behavior=create_prorations`
  - on remove-member, Stripe is modified with the new member
    count (post-remove) and `proration_behavior=create_prorations`
  - when `stripe_subscription_id IS NULL` (free tier), no Stripe
    call is issued — free orgs aren't billable

Pinned behaviors:
  - accept-invite + paid org → Stripe.modify called with quantity
    = members AFTER accept (admin + 1 = 2 in the seeded fixture)
  - accept-invite + free org → Stripe.modify never called
  - remove-member + paid org → Stripe.modify called with quantity
    = members AFTER remove (admin + 0 = 1 in the seeded fixture)
  - remove-member + free org → Stripe.modify never called

The Stripe SDK is mocked end-to-end here (no network). T146's
billing tests cover the wire-level call shape; this test pins
the org-endpoint integration: that the right side-effect happens
at the right moment with the right argument.
"""

from __future__ import annotations

import asyncio
import base64
import secrets
import time
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
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

from alembic import command
from app.deps import get_db, get_jwks_client
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import (
    Organization,
    OrganizationInvitation,
    OrganizationMember,
    User,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

_FAKE_SUB_ID = "sub_test_t168"
_FAKE_ITEM_ID = "si_test_t168_seat"


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
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    keys = _build_keypair()
    engine = make_engine(url)
    factory = make_session_factory(engine)

    async def fake_get_db() -> AsyncGenerator[Any, None]:
        async with factory() as db_session:
            yield db_session

    def fake_jwks_client() -> ClerkJwksClient:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=keys["jwks"])
        return ClerkJwksClient(
            "https://test.clerk",
            transport=httpx.MockTransport(handler),
        )

    app.dependency_overrides[get_db] = fake_get_db
    app.dependency_overrides[get_jwks_client] = fake_jwks_client
    try:
        yield {"keys": keys, "factory": factory}
    finally:
        app.dependency_overrides.clear()


async def _seed_org_with_invite(
    factory: Any,
    *,
    invite_email: str,
    stripe_subscription_id: str | None,
) -> dict[str, Any]:
    """Seeds: admin user + org (with optional stripe_sub_id) +
    pending invite for `invite_email` as role=driver.

    Returns dict with admin_clerk, org_id, token."""
    async with factory() as db:
        admin = User(
            id=uuid6.uuid7(),
            clerk_user_id=f"adm168_{secrets.token_hex(3)}",
            email=f"adm168_{secrets.token_hex(3)}@t",
        )
        db.add(admin)
        await db.flush()
        org = Organization(
            id=uuid6.uuid7(),
            name="T168 Org",
            plan="team",
            admin_user_id=admin.id,
            seats_limit=20,
            stripe_subscription_id=stripe_subscription_id,
        )
        db.add(org)
        await db.flush()
        db.add(
            OrganizationMember(
                id=uuid6.uuid7(),
                org_id=org.id,
                user_id=admin.id,
                role="admin",
            )
        )
        token = secrets.token_urlsafe(32)
        invite = OrganizationInvitation(
            id=uuid6.uuid7(),
            org_id=org.id,
            email=invite_email,
            role="driver",
            token=token,
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
        db.add(invite)
        await db.commit()
        return {
            "admin_clerk": admin.clerk_user_id,
            "admin_email": admin.email,
            "org_id": org.id,
            "token": token,
        }


async def _seed_user_in_org(
    factory: Any, *, org_id: Any, role: str
) -> dict[str, Any]:
    """Add a member to org with given role, returning user info."""
    async with factory() as db:
        user = User(
            id=uuid6.uuid7(),
            clerk_user_id=f"u168_{secrets.token_hex(3)}",
            email=f"u168_{secrets.token_hex(3)}@t",
        )
        db.add(user)
        await db.flush()
        db.add(
            OrganizationMember(
                id=uuid6.uuid7(),
                org_id=org_id,
                user_id=user.id,
                role=role,
            )
        )
        await db.commit()
        return {
            "user_id": user.id,
            "clerk": user.clerk_user_id,
            "email": user.email,
        }


def _hdrs(
    ca: dict[str, Any], clerk: str, email: str
) -> dict[str, str]:
    return {"Authorization": f"Bearer {_bearer(ca['keys'], clerk, email)}"}


# -- accept-invite ----------------------------------------------


def test_accept_invite_paid_org_calls_stripe_with_new_count(
    configured_app: dict[str, Any],
) -> None:
    """When org has stripe_subscription_id, accepting an invite
    must call Stripe.modify with quantity = members AFTER accept.
    Seeded: admin (1). After accept: admin + invitee = 2. Pin:
    Stripe billing follows actual seat count, not stale count."""
    factory = configured_app["factory"]
    invite_email = "invitee168a@t"
    invitee_clerk = "invitee168a_clerk"
    seed = asyncio.run(
        _seed_org_with_invite(
            factory,
            invite_email=invite_email,
            stripe_subscription_id=_FAKE_SUB_ID,
        )
    )

    async def _seed_invitee() -> None:
        async with factory() as db:
            db.add(
                User(
                    id=uuid6.uuid7(),
                    clerk_user_id=invitee_clerk,
                    email=invite_email,
                )
            )
            await db.commit()
    asyncio.run(_seed_invitee())

    fake_sub = {"items": {"data": [{"id": _FAKE_ITEM_ID}]}}
    with patch(
        "infra.stripe.stripe.Subscription.retrieve",
        return_value=fake_sub,
    ), patch(
        "infra.stripe.stripe.Subscription.modify"
    ) as mock_modify, TestClient(app) as client:
        resp = client.post(
            f"/api/v1/orgs/{seed['org_id']}/invites/"
            f"{seed['token']}/accept",
            headers=_hdrs(
                configured_app, invitee_clerk, invite_email
            ),
        )

    assert resp.status_code == 201, resp.text
    mock_modify.assert_called_once()
    kwargs = mock_modify.call_args.kwargs
    assert kwargs["items"][0]["quantity"] == 2
    assert kwargs["proration_behavior"] == "create_prorations"


def test_accept_invite_free_org_does_not_call_stripe(
    configured_app: dict[str, Any],
) -> None:
    """Free-tier orgs (stripe_subscription_id IS NULL) accept
    invites without touching Stripe. Pin: free orgs aren't
    billable; calling Stripe with no sub would 5xx."""
    factory = configured_app["factory"]
    invite_email = "invitee168b@t"
    seed = asyncio.run(
        _seed_org_with_invite(
            factory,
            invite_email=invite_email,
            stripe_subscription_id=None,
        )
    )
    invitee_clerk = "invitee168b_clerk"

    async def _seed() -> None:
        async with factory() as db:
            db.add(
                User(
                    id=uuid6.uuid7(),
                    clerk_user_id=invitee_clerk,
                    email=invite_email,
                )
            )
            await db.commit()
    asyncio.run(_seed())

    with patch(
        "infra.stripe.stripe.Subscription.modify"
    ) as mock_modify, TestClient(app) as client:
        resp = client.post(
            f"/api/v1/orgs/{seed['org_id']}/invites/"
            f"{seed['token']}/accept",
            headers=_hdrs(
                configured_app, invitee_clerk, invite_email
            ),
        )

    assert resp.status_code == 201, resp.text
    mock_modify.assert_not_called()


# -- remove-member ----------------------------------------------


def test_remove_member_paid_org_calls_stripe_with_new_count(
    configured_app: dict[str, Any],
) -> None:
    """When org has stripe_subscription_id, removing a member
    must call Stripe.modify with quantity = members AFTER
    deletion. Seeded: admin + driver = 2. After remove: admin
    only = 1. Pin: Stripe credits the freed seat via proration."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_invite(
            factory,
            invite_email="ignored168c@t",
            stripe_subscription_id=_FAKE_SUB_ID,
        )
    )
    driver = asyncio.run(
        _seed_user_in_org(
            factory, org_id=seed["org_id"], role="driver"
        )
    )

    fake_sub = {"items": {"data": [{"id": _FAKE_ITEM_ID}]}}
    with patch(
        "infra.stripe.stripe.Subscription.retrieve",
        return_value=fake_sub,
    ), patch(
        "infra.stripe.stripe.Subscription.modify"
    ) as mock_modify, TestClient(app) as client:
        resp = client.delete(
            f"/api/v1/orgs/{seed['org_id']}/"
            f"members/{driver['user_id']}",
            headers=_hdrs(
                configured_app,
                seed["admin_clerk"],
                seed["admin_email"],
            ),
        )

    assert resp.status_code == 204, resp.text
    mock_modify.assert_called_once()
    kwargs = mock_modify.call_args.kwargs
    assert kwargs["items"][0]["quantity"] == 1
    assert kwargs["proration_behavior"] == "create_prorations"


def test_remove_member_free_org_does_not_call_stripe(
    configured_app: dict[str, Any],
) -> None:
    """Free-tier remove-member doesn't touch Stripe. Pin: same
    invariant as accept-invite — free orgs aren't billable."""
    factory = configured_app["factory"]
    seed = asyncio.run(
        _seed_org_with_invite(
            factory,
            invite_email="ignored168d@t",
            stripe_subscription_id=None,
        )
    )
    driver = asyncio.run(
        _seed_user_in_org(
            factory, org_id=seed["org_id"], role="driver"
        )
    )

    with patch(
        "infra.stripe.stripe.Subscription.modify"
    ) as mock_modify, TestClient(app) as client:
        resp = client.delete(
            f"/api/v1/orgs/{seed['org_id']}/"
            f"members/{driver['user_id']}",
            headers=_hdrs(
                configured_app,
                seed["admin_clerk"],
                seed["admin_email"],
            ),
        )

    assert resp.status_code == 204, resp.text
    mock_modify.assert_not_called()
