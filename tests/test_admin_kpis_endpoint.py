"""T178 — `GET /api/v1/admin/kpis` returns the three glance-able
KPIs the operator monitors: WPD (Weekly Paid Drivers), activation
rate (signups in last 7d that uploaded a session), and upload
success rate (sessions in last 7d that reached `ready` status).

Approximations of the canonical ADR definitions — the full
versions need PostHog event joins. This endpoint is the local-DB
truth: cheap to query, no cross-system latency.
"""

import asyncio
import base64
import time
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
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
from app.config import Config as AppConfig
from app.deps import get_config, get_db, get_jwks_client
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, Session, User

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


async def _seed_admin(factory: Any, sub: str, email: str) -> None:
    async with factory() as db:
        db.add(
            User(
                id=uuid6.uuid7(),
                clerk_user_id=sub,
                email=email,
                is_admin=True,
            )
        )
        await db.commit()


async def _seed_user_with_sessions(
    factory: Any,
    *,
    user_age_days: int,
    subscription_status: str,
    session_specs: list[tuple[int, str]],  # (age_days, status) tuples
) -> None:
    """Seed a User+Driver pair, plus N Sessions with given ages/statuses."""
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    now = datetime.now(UTC)
    user_created = now - timedelta(days=user_age_days)
    async with factory() as db:
        db.add(
            User(
                id=user_id,
                clerk_user_id=f"u_{user_id.hex}",
                email=f"{user_id.hex}@test",
                created_at=user_created,
                subscription_status=subscription_status,
            )
        )
        db.add(
            DriverProfile(
                id=driver_id, user_id=user_id, display_name="A"
            )
        )
        for age_days, sess_status in session_specs:
            db.add(
                Session(
                    id=uuid6.uuid7(),
                    driver_id=driver_id,
                    manifest={},
                    status=sess_status,
                    created_at=now - timedelta(days=age_days),
                )
            )
        await db.commit()


def test_kpis_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    resp = TestClient(app).get("/api/v1/admin/kpis")
    assert resp.status_code == 401


async def test_kpis_non_admin_returns_403(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    async with factory() as db:
        db.add(
            User(
                id=uuid6.uuid7(),
                clerk_user_id="u_plain",
                email="plain@test",
                is_admin=False,
            )
        )
        await db.commit()
    token = _bearer(keys, "u_plain", "plain@test")
    resp = TestClient(app).get(
        "/api/v1/admin/kpis",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_kpis_empty_db_returns_zeroes(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_admin(factory, "u_admin_empty", "admin_empty@test")
    token = _bearer(keys, "u_admin_empty", "admin_empty@test")
    resp = TestClient(app).get(
        "/api/v1/admin/kpis",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["wpd"] == 0
    # Activation: 1 user (the admin) created in last 7d, 0 with
    # sessions → 0/1 = 0.0.
    assert body["activation_rate"] == 0.0
    # Upload success: vacuously 1.0 when there are no sessions.
    assert body["upload_success_rate"] == 1.0
    assert "computed_at" in body


async def test_kpis_with_seeded_data(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_admin(factory, "u_admin_kpi", "admin_kpi@test")

    # Paid driver in last 7d → counts toward WPD.
    await _seed_user_with_sessions(
        factory,
        user_age_days=10,
        subscription_status="active",
        session_specs=[(2, "ready")],
    )
    # Paid driver but no recent session → NOT counted.
    await _seed_user_with_sessions(
        factory,
        user_age_days=10,
        subscription_status="active",
        session_specs=[(20, "ready")],
    )
    # Trial driver with a recent session → counts toward WPD.
    await _seed_user_with_sessions(
        factory,
        user_age_days=3,
        subscription_status="trialing",
        session_specs=[(1, "ready")],
    )
    # Inactive driver with recent session → NOT paid → no WPD bump.
    await _seed_user_with_sessions(
        factory,
        user_age_days=2,
        subscription_status="inactive",
        session_specs=[(1, "ready")],
    )
    # Recent signup, no session → drags activation rate.
    await _seed_user_with_sessions(
        factory,
        user_age_days=1,
        subscription_status="inactive",
        session_specs=[],
    )
    # Failed session → drags upload success rate.
    await _seed_user_with_sessions(
        factory,
        user_age_days=10,
        subscription_status="active",
        session_specs=[(1, "failed")],
    )

    token = _bearer(keys, "u_admin_kpi", "admin_kpi@test")
    resp = TestClient(app).get(
        "/api/v1/admin/kpis",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # WPD = active/trialing users with ≥1 session in last 7d:
    # paid+recent (1) + trialing+recent (1) = 2.
    # The active user with recent failed session also counts —
    # status filter is on subscription, not session quality.
    assert body["wpd"] == 3
    # Activation denom: users created in last 7d = admin + trialing
    # + inactive_recent + recent_signup = 4. Numerator: trialing
    # and inactive_recent each uploaded a session = 2. 2/4 = 0.5.
    assert body["activation_rate"] == 0.5
    # Upload success: sessions in last 7d = 4 (3 ready + 1 failed). 3/4 = 0.75.
    assert body["upload_success_rate"] == 0.75


async def _no_event_loop_marker() -> None:  # noqa: PLR0917
    # Sentinel used to silence asyncio.run-in-fixture warnings if
    # any future test refactor accidentally moves logic into a sync
    # fixture. Currently unused; removable when fixture style is
    # codified across the suite.
    await asyncio.sleep(0)
