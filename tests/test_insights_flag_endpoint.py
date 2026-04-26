"""T011 (sprint2-backend) — POST /api/v1/insights/{id}/flag.

Canonical URL for the user-flagged false-positive endpoint. Same
mutation as the legacy /api/v1/coaching/insights/{id}/flag (T205);
T011 promotes the endpoint out of the /coaching subtree so it's
addressable as a top-level resource.

Cross-driver isolation: 404 (not 403) for absent or not-owned
insights — mirrors `sessions.get_session` posture.

Idempotent: subsequent flags are 200 no-ops so a double-tap UI
doesn't double-count in the rule-FP aggregation.

The "decrements rule confidence" outcome (sprint task wording) is
implicit: setting `flagged_false_positive` raises the rule's
flagged-rate when surfaced through the existing aggregation
(`compute_high_false_positive_rules`). T011 verifies both the
endpoint mutation AND that the aggregation picks it up.
"""

from __future__ import annotations

import asyncio
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
from sqlalchemy import select

from alembic import command
from app.deps import get_db, get_jwks_client
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.coaching_flag_aggregation import compute_high_false_positive_rules
from infra.db import make_engine, make_session_factory
from infra.orm import CoachingInsight, DriverProfile, User

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
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": _b64u(n),
        "e": _b64u(e),
    }
    return {"priv_pem": priv_pem, "kid": kid, "jwks": {"keys": [jwk]}}


def _sign(priv_pem: bytes, claims: dict[str, Any], kid: str) -> str:
    return jwt.encode(claims, priv_pem, algorithm="RS256", headers={"kid": kid})


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
            "https://test.clerk", transport=httpx.MockTransport(handler)
        )

    app.dependency_overrides[get_db] = fake_get_db
    app.dependency_overrides[get_jwks_client] = fake_jwks_client
    try:
        yield {"keys": keys, "factory": factory}
    finally:
        app.dependency_overrides.clear()


def _bearer(keys: dict[str, Any], clerk_user_id: str) -> str:
    return _sign(
        keys["priv_pem"],
        {
            "sub": clerk_user_id,
            "email": f"{clerk_user_id}@t",
            "exp": int(time.time()) + 3600,
        },
        keys["kid"],
    )


async def _seed_insight(
    factory: Any, *, clerk_user_id: str, rule_id: str = "rule.x.v1"
) -> dict[str, Any]:
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    sid = uuid6.uuid7()
    iid = uuid6.uuid7()
    async with factory() as db:
        db.add(
            User(
                id=user_id,
                clerk_user_id=clerk_user_id,
                email=f"{clerk_user_id}@t",
            )
        )
        db.add(
            DriverProfile(id=driver_id, user_id=user_id, display_name="A")
        )
        db.add(
            CoachingInsight(
                id=iid,
                session_id=sid,
                driver_id=driver_id,
                created_at=datetime(2026, 4, 25, tzinfo=UTC),
                rule_id=rule_id,
                rule_version="1",
                observation="late release",
                evidence=[],
                severity="medium",
                confidence=0.8,
            )
        )
        await db.commit()
    return {"insight_id": iid, "driver_id": driver_id, "rule_id": rule_id}


def _flag(client: TestClient, ca: dict[str, Any], who: str, iid: Any) -> Any:
    return client.post(
        f"/api/v1/insights/{iid}/flag",
        headers={"Authorization": f"Bearer {_bearer(ca['keys'], who)}"},
    )


# -- behavior ------------------------------------------------------


def test_flag_sets_false_positive(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_insight(configured_app["factory"], clerk_user_id="u_f")
    )
    client = TestClient(app)
    resp = _flag(client, configured_app, "u_f", seeded["insight_id"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(seeded["insight_id"])
    assert body["flagged_false_positive"] is True


def test_flag_persists_to_db(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_insight(configured_app["factory"], clerk_user_id="u_p")
    )
    client = TestClient(app)
    _flag(client, configured_app, "u_p", seeded["insight_id"])

    async def _read() -> bool:
        async with configured_app["factory"]() as db:
            row = (
                await db.execute(
                    select(CoachingInsight).where(
                        CoachingInsight.id == seeded["insight_id"]
                    )
                )
            ).scalar_one()
            return row.flagged_false_positive

    assert asyncio.run(_read()) is True


def test_flag_is_idempotent(
    configured_app: dict[str, Any],
) -> None:
    """Re-flagging a flagged insight is a 200 no-op so a double-tap
    UI doesn't double-count in the rule-FP aggregation."""
    seeded = asyncio.run(
        _seed_insight(configured_app["factory"], clerk_user_id="u_i")
    )
    client = TestClient(app)
    r1 = _flag(client, configured_app, "u_i", seeded["insight_id"])
    r2 = _flag(client, configured_app, "u_i", seeded["insight_id"])
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json() == r2.json()


def test_flag_decrements_rule_confidence_via_aggregation(
    configured_app: dict[str, Any],
) -> None:
    """End-to-end: flagging the only insight for a rule pushes
    that rule's FP rate to 1.0, surfacing it via
    `compute_high_false_positive_rules`."""
    seeded = asyncio.run(
        _seed_insight(
            configured_app["factory"],
            clerk_user_id="u_agg",
            rule_id="rule.flagged.v1",
        )
    )
    client = TestClient(app)
    _flag(client, configured_app, "u_agg", seeded["insight_id"])

    async def _agg() -> list[Any]:
        async with configured_app["factory"]() as db:
            return list(await compute_high_false_positive_rules(db))

    stats = asyncio.run(_agg())
    matching = [s for s in stats if s.rule_id == "rule.flagged.v1"]
    assert len(matching) == 1
    assert matching[0].false_positive_rate == 1.0
    assert matching[0].flagged_count == 1


def test_flag_other_drivers_insight_returns_404(
    configured_app: dict[str, Any],
) -> None:
    asyncio.run(
        _seed_insight(configured_app["factory"], clerk_user_id="u_a")
    )
    other = asyncio.run(
        _seed_insight(configured_app["factory"], clerk_user_id="u_b")
    )
    client = TestClient(app)
    resp = _flag(client, configured_app, "u_a", other["insight_id"])
    assert resp.status_code == 404


def test_flag_unknown_insight_returns_404(
    configured_app: dict[str, Any],
) -> None:
    asyncio.run(
        _seed_insight(configured_app["factory"], clerk_user_id="u_x")
    )
    client = TestClient(app)
    resp = _flag(client, configured_app, "u_x", uuid6.uuid7())
    assert resp.status_code == 404


@pytest.mark.usefixtures("configured_app")
def test_flag_unauthenticated_returns_401() -> None:
    client = TestClient(app)
    resp = client.post(f"/api/v1/insights/{uuid6.uuid7()}/flag")
    assert resp.status_code == 401
