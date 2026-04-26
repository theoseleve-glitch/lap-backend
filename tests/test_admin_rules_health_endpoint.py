"""T013 (sprint2-backend) — GET /api/v1/admin/rules/health.

Admin overview listing every rule in the library with its
false-positive rate computed from `coaching_insights`. Rules
that haven't fired yet still appear with `total=0`, so dead
or never-fired rules surface alongside misbehaving ones.

Distinct from T206's /coaching/rules/flagged which only returns
rules above the 10% FP threshold. T013 is the comprehensive
view; T206 is the alert subset.

Auth posture matches the rest of /admin: 401 unauth, 403 non-
admin, 200 admin.

Sort order: FP rate desc so worst offenders appear first.
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
from coaching.library.rule_loader import load_rules
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import CoachingInsight, DriverProfile, Session, User

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
RULES_DIR = REPO_ROOT / "coaching" / "library" / "rules"


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


# -- behavior ------------------------------------------------------


def test_rules_health_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    resp = TestClient(app).get("/api/v1/admin/rules/health")
    assert resp.status_code == 401


async def test_rules_health_non_admin_returns_403(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    async with factory() as db:
        db.add(
            User(
                id=uuid6.uuid7(),
                clerk_user_id="u_plain_rh",
                email="plain_rh@test",
                is_admin=False,
            )
        )
        await db.commit()
    token = _bearer(keys, "u_plain_rh", "plain_rh@test")
    resp = TestClient(app).get(
        "/api/v1/admin/rules/health",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_rules_health_admin_no_data_returns_all_rules_at_zero(
    configured_app: dict[str, Any],
) -> None:
    """With no insights, every library rule still appears — at
    0% rate / 0 counts. Lets the operator spot rules that have
    never fired (dead code in the library)."""
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_admin(factory, "u_admin_rh_z", "admin_rh_z@test")
    token = _bearer(keys, "u_admin_rh_z", "admin_rh_z@test")
    resp = TestClient(app).get(
        "/api/v1/admin/rules/health",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    library = load_rules(RULES_DIR)
    assert len(body["rules"]) == len(library)
    for entry in body["rules"]:
        assert entry["total"] == 0
        assert entry["flagged_count"] == 0
        assert entry["false_positive_rate"] == 0.0


async def test_rules_health_includes_stats_for_fired_rule(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_admin(factory, "u_admin_rh_f", "admin_rh_f@test")
    library_rule_id = load_rules(RULES_DIR)[0].id
    await _seed_insights_for_rule(
        factory, library_rule_id, total=10, flagged=3
    )
    token = _bearer(keys, "u_admin_rh_f", "admin_rh_f@test")
    resp = TestClient(app).get(
        "/api/v1/admin/rules/health",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()
    matching = [r for r in body["rules"] if r["rule_id"] == library_rule_id]
    assert len(matching) == 1
    rule = matching[0]
    assert rule["total"] == 10
    assert rule["flagged_count"] == 3
    assert rule["false_positive_rate"] == pytest.approx(0.3)


async def test_rules_health_sorted_worst_fp_first(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_admin(factory, "u_admin_rh_s", "admin_rh_s@test")
    library = load_rules(RULES_DIR)
    rid_low = library[0].id
    rid_high = library[1].id
    await _seed_insights_for_rule(factory, rid_low, total=100, flagged=10)
    await _seed_insights_for_rule(factory, rid_high, total=10, flagged=8)
    token = _bearer(keys, "u_admin_rh_s", "admin_rh_s@test")
    resp = TestClient(app).get(
        "/api/v1/admin/rules/health",
        headers={"Authorization": f"Bearer {token}"},
    )
    rules = resp.json()["rules"]
    pos_high = next(i for i, r in enumerate(rules) if r["rule_id"] == rid_high)
    pos_low = next(i for i, r in enumerate(rules) if r["rule_id"] == rid_low)
    assert pos_high < pos_low  # 80% before 10%


async def test_rules_health_includes_unknown_rule_ids_from_db(
    configured_app: dict[str, Any],
) -> None:
    """A rule_id that fired insights but doesn't (any longer)
    exist in the library still surfaces — operators need to spot
    stale insight data after a rule rename/removal."""
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_admin(factory, "u_admin_rh_u", "admin_rh_u@test")
    await _seed_insights_for_rule(
        factory, "rule.removed.v1", total=5, flagged=2
    )
    token = _bearer(keys, "u_admin_rh_u", "admin_rh_u@test")
    resp = TestClient(app).get(
        "/api/v1/admin/rules/health",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()
    rule_ids = [r["rule_id"] for r in body["rules"]]
    assert "rule.removed.v1" in rule_ids
