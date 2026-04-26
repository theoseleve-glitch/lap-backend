"""T222 — `GET /api/v1/driver/:id/outcomes?window=Nd`.

Driver-facing read of recent ActionOutcome rows for the
post-session dashboard. The `window` param is a "last N days"
filter on `measured_at`; defaults to 30d if omitted.

Response includes a `mean_efficacy` summary so the dashboard's
headline number agrees with the rows exactly — clients should
not re-aggregate the items list.

Auth posture: 404 if you ask for a driver_id that isn't yours
(matches T217's list endpoint and `sessions.get_session`).
Malformed window strings return 422 — the contract is "Nd"
where N is a positive integer.
"""

import base64
import time
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
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
from sqlalchemy.ext.asyncio import AsyncSession

from alembic import command
from app.config import Config as AppConfig
from app.deps import get_config, get_db, get_jwks_client
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import (
    ActionOutcome,
    ActionPrescription,
    DriverProfile,
    User,
    WeaknessEpisode,
)

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


async def _seed_user_with_driver(
    factory: Any, *, sub: str, email: str
) -> tuple[UUID, UUID, UUID]:
    """Returns (user_id, driver_id, episode_id)."""
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    episode_id = uuid6.uuid7()
    now = datetime.now(UTC)
    async with factory() as db:
        db.add(User(id=user_id, clerk_user_id=sub, email=email))
        db.add(
            DriverProfile(
                id=driver_id, user_id=user_id, display_name="A"
            )
        )
        db.add(
            WeaknessEpisode(
                id=episode_id, driver_id=driver_id,
                category="technique", subcategory="late_braking",
                first_observed_at=now, last_observed_at=now,
                recurrence_count=1, sessions_affected=[],
                severity="medium", confidence=1.0, status="open",
                linked_prescriptions=[], linked_insights=[],
                library_rule_id="br.late_braking.apex_speed_loss",
            )
        )
        await db.commit()
    return user_id, driver_id, episode_id


async def _add_outcome(
    factory: Any,
    *,
    driver_id: UUID,
    episode_id: UUID,
    measured_at: datetime,
    efficacy: float,
) -> UUID:
    p_id = uuid6.uuid7()
    o_id = uuid6.uuid7()
    now = datetime.now(UTC)
    async with factory() as db:
        db.add(
            ActionPrescription(
                id=p_id, driver_id=driver_id,
                weakness_episode_id=episode_id, created_at=now,
                priority="p1", title="t", rationale="r",
                expected_context={}, success_condition={},
                status="completed", drill_template_id="drill.x",
                outcome_id=o_id,
            )
        )
        db.add(
            ActionOutcome(
                id=o_id, prescription_id=p_id,
                measured_at=measured_at,
                session_ids_measured=[],
                success_condition_met=(efficacy >= 0.5),
                measured_value=96.0, delta_vs_baseline=-4.0,
                confidence_change=-0.5,
                prescription_efficacy_score=efficacy,
            )
        )
        await db.commit()
    return o_id


def test_outcomes_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    fake_id = uuid6.uuid7()
    resp = TestClient(app).get(
        f"/api/v1/driver/{fake_id}/outcomes"
    )
    assert resp.status_code == 401


async def test_outcomes_other_drivers_id_returns_404(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    _, driver_id_a, _ = await _seed_user_with_driver(
        factory, sub="u_oc_a", email="oc_a@test"
    )
    async with factory() as db:
        u_id = uuid6.uuid7()
        db.add(User(id=u_id, clerk_user_id="u_oc_b", email="oc_b@test"))
        db.add(
            DriverProfile(
                id=uuid6.uuid7(), user_id=u_id, display_name="B"
            )
        )
        await db.commit()
    token = _bearer(keys, "u_oc_b", "oc_b@test")
    resp = TestClient(app).get(
        f"/api/v1/driver/{driver_id_a}/outcomes",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


async def test_outcomes_returns_empty_with_zero_total(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    _, driver_id, _ = await _seed_user_with_driver(
        factory, sub="u_oc_empty", email="oc_empty@test"
    )
    token = _bearer(keys, "u_oc_empty", "oc_empty@test")
    resp = TestClient(app).get(
        f"/api/v1/driver/{driver_id}/outcomes",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["mean_efficacy"] is None
    assert body["window_days"] == 30


async def test_outcomes_default_window_is_30_days(
    configured_app: dict[str, Any],
) -> None:
    """Omitting ?window defaults to 30d. Older outcomes drop out."""
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    _, driver_id, episode = await _seed_user_with_driver(
        factory, sub="u_oc_def", email="oc_def@test"
    )
    now = datetime.now(UTC)
    inside = await _add_outcome(
        factory, driver_id=driver_id, episode_id=episode,
        measured_at=now - timedelta(days=10), efficacy=1.0,
    )
    await _add_outcome(
        factory, driver_id=driver_id, episode_id=episode,
        measured_at=now - timedelta(days=45), efficacy=0.0,
    )
    token = _bearer(keys, "u_oc_def", "oc_def@test")
    resp = TestClient(app).get(
        f"/api/v1/driver/{driver_id}/outcomes",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == str(inside)


async def test_outcomes_window_param_filters_inclusively(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    _, driver_id, episode = await _seed_user_with_driver(
        factory, sub="u_oc_w7", email="oc_w7@test"
    )
    now = datetime.now(UTC)
    in_window = await _add_outcome(
        factory, driver_id=driver_id, episode_id=episode,
        measured_at=now - timedelta(days=3), efficacy=1.0,
    )
    await _add_outcome(
        factory, driver_id=driver_id, episode_id=episode,
        measured_at=now - timedelta(days=10), efficacy=0.0,
    )
    token = _bearer(keys, "u_oc_w7", "oc_w7@test")
    resp = TestClient(app).get(
        f"/api/v1/driver/{driver_id}/outcomes?window=7d",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["window_days"] == 7
    assert body["total"] == 1
    assert body["items"][0]["id"] == str(in_window)


async def test_outcomes_includes_mean_efficacy_summary(
    configured_app: dict[str, Any],
) -> None:
    """Two outcomes within window: efficacy 1.0 and 0.0 → mean 0.5."""
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    _, driver_id, episode = await _seed_user_with_driver(
        factory, sub="u_oc_mean", email="oc_mean@test"
    )
    now = datetime.now(UTC)
    await _add_outcome(
        factory, driver_id=driver_id, episode_id=episode,
        measured_at=now - timedelta(days=2), efficacy=1.0,
    )
    await _add_outcome(
        factory, driver_id=driver_id, episode_id=episode,
        measured_at=now - timedelta(days=5), efficacy=0.0,
    )
    token = _bearer(keys, "u_oc_mean", "oc_mean@test")
    resp = TestClient(app).get(
        f"/api/v1/driver/{driver_id}/outcomes?window=30d",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["mean_efficacy"] == pytest.approx(0.5)


async def test_outcomes_excludes_other_drivers(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    _, driver_a, ep_a = await _seed_user_with_driver(
        factory, sub="u_oc_only_a", email="only_a@test"
    )
    _, driver_b, ep_b = await _seed_user_with_driver(
        factory, sub="u_oc_only_b", email="only_b@test"
    )
    now = datetime.now(UTC)
    await _add_outcome(
        factory, driver_id=driver_a, episode_id=ep_a,
        measured_at=now, efficacy=1.0,
    )
    await _add_outcome(
        factory, driver_id=driver_b, episode_id=ep_b,
        measured_at=now, efficacy=0.0,
    )
    token = _bearer(keys, "u_oc_only_a", "only_a@test")
    resp = TestClient(app).get(
        f"/api/v1/driver/{driver_a}/outcomes",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1


async def test_outcomes_malformed_window_returns_422(
    configured_app: dict[str, Any],
) -> None:
    """Contract is Nd; 'forever' or '7w' or 'abc' → 422."""
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    _, driver_id, _ = await _seed_user_with_driver(
        factory, sub="u_oc_bad", email="bad@test"
    )
    token = _bearer(keys, "u_oc_bad", "bad@test")
    resp = TestClient(app).get(
        f"/api/v1/driver/{driver_id}/outcomes?window=7w",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422
