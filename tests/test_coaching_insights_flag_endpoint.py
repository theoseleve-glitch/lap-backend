"""T205 — `POST /api/v1/coaching/insights/:id/flag`.

User-facing false-positive flag. The driver who owns the
session that produced the insight POSTs to this endpoint and
the server flips `coaching_insights.flagged_false_positive` to
true. No body needed — the URL and the authenticated user
identify everything.

T206 (next) aggregates these flags per rule_id and surfaces
rules with >10% false-positive rate to admins. So this endpoint
is the data-collection half of a feedback loop: the only signal
flowing here is "users said this rule misfired."

Auth posture mirrors `sessions.py:get_session` — 404 (not 403)
for both "doesn't exist" and "exists but isn't yours" so the
endpoint doesn't leak insight IDs across users.
"""

import base64
import time
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import jwt
import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alembic import command
from app.config import Config as AppConfig
from app.deps import get_config, get_db, get_jwks_client
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import CoachingInsight, DriverProfile, Session, User

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


async def _seed_user_with_insight(
    factory: Any, *, sub: str, email: str
) -> tuple[UUID, UUID]:
    """Seed User + DriverProfile + Session + CoachingInsight.
    Returns (user_id, insight_id) for the test to act on."""
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    session_id = uuid6.uuid7()
    insight_id = uuid6.uuid7()
    now = datetime.now(UTC)
    async with factory() as db:
        db.add(User(id=user_id, clerk_user_id=sub, email=email))
        db.add(
            DriverProfile(
                id=driver_id, user_id=user_id, display_name="A"
            )
        )
        db.add(
            Session(
                id=session_id, driver_id=driver_id,
                manifest={}, status="ready", created_at=now,
            )
        )
        db.add(
            CoachingInsight(
                id=insight_id, session_id=session_id,
                driver_id=driver_id, created_at=now,
                rule_id="br.late_braking.apex_speed_loss",
                rule_version="1", observation="apex deficit",
                evidence=["lap_3"], severity="medium", confidence=0.85,
            )
        )
        await db.commit()
    return user_id, insight_id


async def _read_flag(factory: Any, insight_id: UUID) -> bool:
    async with factory() as db:
        row = (
            await db.execute(
                select(CoachingInsight).where(
                    CoachingInsight.id == insight_id
                )
            )
        ).scalar_one()
        return row.flagged_false_positive


def test_flag_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    fake_id = uuid6.uuid7()
    resp = TestClient(app).post(
        f"/api/v1/coaching/insights/{fake_id}/flag"
    )
    assert resp.status_code == 401


async def test_flag_unknown_insight_returns_404(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    async with factory() as db:
        db.add(
            User(
                id=uuid6.uuid7(),
                clerk_user_id="u_no_insight",
                email="ni@test",
            )
        )
        await db.commit()
    token = _bearer(keys, "u_no_insight", "ni@test")
    fake_id = uuid6.uuid7()
    resp = TestClient(app).post(
        f"/api/v1/coaching/insights/{fake_id}/flag",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


async def test_flag_insight_owned_by_other_user_returns_404(
    configured_app: dict[str, Any],
) -> None:
    """Non-owners must see 404 — same posture as get_session
    (sessions.py). Returning 403 would leak that the insight ID
    exists somewhere in the system."""
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    _, insight_id = await _seed_user_with_insight(
        factory, sub="u_owner", email="owner@test"
    )
    # Seed a second user and try to flag the first user's insight.
    async with factory() as db:
        db.add(
            User(
                id=uuid6.uuid7(),
                clerk_user_id="u_intruder",
                email="intruder@test",
            )
        )
        await db.commit()
    token = _bearer(keys, "u_intruder", "intruder@test")
    resp = TestClient(app).post(
        f"/api/v1/coaching/insights/{insight_id}/flag",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
    assert await _read_flag(factory, insight_id) is False


async def test_flag_persists_to_database(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    _, insight_id = await _seed_user_with_insight(
        factory, sub="u_owner_w", email="owner_w@test"
    )
    assert await _read_flag(factory, insight_id) is False
    token = _bearer(keys, "u_owner_w", "owner_w@test")
    resp = TestClient(app).post(
        f"/api/v1/coaching/insights/{insight_id}/flag",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert await _read_flag(factory, insight_id) is True


async def test_flag_returns_flagged_state_in_body(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    _, insight_id = await _seed_user_with_insight(
        factory, sub="u_owner_b", email="owner_b@test"
    )
    token = _bearer(keys, "u_owner_b", "owner_b@test")
    resp = TestClient(app).post(
        f"/api/v1/coaching/insights/{insight_id}/flag",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(insight_id)
    assert body["flagged_false_positive"] is True


async def test_flag_is_idempotent(
    configured_app: dict[str, Any],
) -> None:
    """Calling twice must leave the row in the same state — no
    counter, no duplicate row, just the bool stays true. Cheap
    invariant that protects T206's aggregation from over-counting
    if a client double-clicks."""
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    _, insight_id = await _seed_user_with_insight(
        factory, sub="u_owner_i", email="owner_i@test"
    )
    token = _bearer(keys, "u_owner_i", "owner_i@test")
    headers = {"Authorization": f"Bearer {token}"}
    url = f"/api/v1/coaching/insights/{insight_id}/flag"
    r1 = TestClient(app).post(url, headers=headers)
    r2 = TestClient(app).post(url, headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert await _read_flag(factory, insight_id) is True
