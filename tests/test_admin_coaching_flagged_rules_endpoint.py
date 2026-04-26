"""T206 — `GET /api/v1/admin/coaching/rules/flagged`.

Surfaces the output of `compute_high_false_positive_rules` to
operators. Same fixture/auth posture as test_admin_kpis_endpoint:
unauth → 401, non-admin → 403, admin sees the aggregated stats.
"""

import base64
import time
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
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


async def _seed_insights_for_rule(
    factory: Any, rule_id: str, *, total: int, flagged: int
) -> None:
    """Stand up a driver+session and N insights for one rule_id."""
    async with factory() as db:
        user_id = uuid6.uuid7()
        driver_id = uuid6.uuid7()
        session_id = uuid6.uuid7()
        now = datetime.now(UTC)
        db.add(
            User(
                id=user_id,
                clerk_user_id=f"u_{user_id.hex}",
                email=f"{user_id.hex}@test",
            )
        )
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
        for i in range(total):
            db.add(
                CoachingInsight(
                    id=uuid6.uuid7(), session_id=session_id,
                    driver_id=driver_id, created_at=now,
                    rule_id=rule_id, rule_version="1",
                    observation="x", evidence=[], severity="low",
                    confidence=0.5,
                    flagged_false_positive=(i < flagged),
                )
            )
        await db.commit()


def test_flagged_rules_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    resp = TestClient(app).get(
        "/api/v1/admin/coaching/rules/flagged"
    )
    assert resp.status_code == 401


async def test_flagged_rules_non_admin_returns_403(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    async with factory() as db:
        db.add(
            User(
                id=uuid6.uuid7(),
                clerk_user_id="u_plain_fr",
                email="plain_fr@test",
                is_admin=False,
            )
        )
        await db.commit()
    token = _bearer(keys, "u_plain_fr", "plain_fr@test")
    resp = TestClient(app).get(
        "/api/v1/admin/coaching/rules/flagged",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_flagged_rules_admin_with_no_data_returns_empty(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_admin(factory, "u_admin_fr_empty", "admin_fr_empty@test")
    token = _bearer(keys, "u_admin_fr_empty", "admin_fr_empty@test")
    resp = TestClient(app).get(
        "/api/v1/admin/coaching/rules/flagged",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"rules": []}


async def test_flagged_rules_returns_rule_above_threshold(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_admin(factory, "u_admin_fr_one", "admin_fr_one@test")
    await _seed_insights_for_rule(
        factory, "br.misfires", total=100, flagged=15
    )
    token = _bearer(keys, "u_admin_fr_one", "admin_fr_one@test")
    resp = TestClient(app).get(
        "/api/v1/admin/coaching/rules/flagged",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["rules"]) == 1
    rule = body["rules"][0]
    assert rule["rule_id"] == "br.misfires"
    assert rule["total"] == 100
    assert rule["flagged_count"] == 15
    assert rule["false_positive_rate"] == pytest.approx(0.15)


async def test_flagged_rules_excludes_below_threshold(
    configured_app: dict[str, Any],
) -> None:
    """A rule at exactly 10% must NOT appear (strict `>` boundary
    is the contract pinned by the aggregation tests)."""
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_admin(factory, "u_admin_fr_b", "admin_fr_b@test")
    await _seed_insights_for_rule(
        factory, "br.borderline", total=10, flagged=1
    )
    token = _bearer(keys, "u_admin_fr_b", "admin_fr_b@test")
    resp = TestClient(app).get(
        "/api/v1/admin/coaching/rules/flagged",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.json() == {"rules": []}


async def test_flagged_rules_sorted_worst_first(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_admin(factory, "u_admin_fr_s", "admin_fr_s@test")
    await _seed_insights_for_rule(
        factory, "br.bad15", total=100, flagged=15  # 15%
    )
    await _seed_insights_for_rule(
        factory, "br.bad50", total=10, flagged=5  # 50%
    )
    await _seed_insights_for_rule(
        factory, "br.bad22", total=50, flagged=11  # 22%
    )
    token = _bearer(keys, "u_admin_fr_s", "admin_fr_s@test")
    resp = TestClient(app).get(
        "/api/v1/admin/coaching/rules/flagged",
        headers={"Authorization": f"Bearer {token}"},
    )
    rule_order = [r["rule_id"] for r in resp.json()["rules"]]
    assert rule_order == ["br.bad50", "br.bad22", "br.bad15"]
