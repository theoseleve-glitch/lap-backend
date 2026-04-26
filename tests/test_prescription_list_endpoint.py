"""T217 — `GET /api/v1/driver/:id/prescriptions?status=open`.

Driver-facing list of prescriptions filtered by status. The
home screen's "what should I work on" surface — clients hit
`?status=open` to get everything still actionable (proposed +
accepted + in_progress) and skip the terminal states (completed,
skipped, expired).

Auth posture matches T215/T216: 404 if you ask for a driver_id
that isn't yours, so we don't leak which driver_ids exist.
Non-existent driver and someone-else's driver collapse to the
same response.

`status` defaults to "open" when omitted (the only value the
home screen ever asks for); other status values filter exactly
(e.g. `?status=skipped` returns only skipped). Unknown values
return empty rather than 422 — the endpoint is best-effort and
unknown filters are non-fatal client errors.
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
from sqlalchemy.ext.asyncio import AsyncSession

from alembic import command
from app.config import Config as AppConfig
from app.deps import get_config, get_db, get_jwks_client
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import (
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


async def _add_prescription(
    factory: Any,
    *,
    driver_id: UUID,
    episode_id: UUID,
    status: str,
) -> UUID:
    new_id = uuid6.uuid7()
    now = datetime.now(UTC)
    async with factory() as db:
        db.add(
            ActionPrescription(
                id=new_id, driver_id=driver_id,
                weakness_episode_id=episode_id, created_at=now,
                priority="p1", title="Address late braking",
                rationale="r", expected_context={},
                success_condition={
                    "metric": "apex_speed",
                    "comparison": "gte",
                    "target_pct": 0.96,
                    "measurement_window": "next_3_sessions",
                }, status=status,
                drill_template_id="drill.x",
            )
        )
        await db.commit()
    return new_id


def test_list_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    fake_id = uuid6.uuid7()
    resp = TestClient(app).get(
        f"/api/v1/driver/{fake_id}/prescriptions"
    )
    assert resp.status_code == 401


async def test_list_other_drivers_id_returns_404(
    configured_app: dict[str, Any],
) -> None:
    """Cross-driver enumeration must collapse to 404."""
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    _, driver_id_a, _ = await _seed_user_with_driver(
        factory, sub="u_other_a", email="other_a@test"
    )
    async with factory() as db:
        db.add(
            User(
                id=uuid6.uuid7(),
                clerk_user_id="u_other_b",
                email="other_b@test",
            )
        )
        db.add(
            DriverProfile(
                id=uuid6.uuid7(),
                user_id=(
                    await db.execute(
                        __import__("sqlalchemy").select(User.id).where(
                            User.clerk_user_id == "u_other_b"
                        )
                    )
                ).scalar_one(),
                display_name="B",
            )
        )
        await db.commit()
    token = _bearer(keys, "u_other_b", "other_b@test")
    resp = TestClient(app).get(
        f"/api/v1/driver/{driver_id_a}/prescriptions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


async def test_list_returns_empty_when_no_prescriptions(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    _, driver_id, _ = await _seed_user_with_driver(
        factory, sub="u_empty", email="empty@test"
    )
    token = _bearer(keys, "u_empty", "empty@test")
    resp = TestClient(app).get(
        f"/api/v1/driver/{driver_id}/prescriptions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"items": [], "total": 0}


async def test_list_open_returns_proposed_and_accepted(
    configured_app: dict[str, Any],
) -> None:
    """status=open is the union of actionable states."""
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    _, driver_id, episode_id = await _seed_user_with_driver(
        factory, sub="u_open", email="open@test"
    )
    p_proposed = await _add_prescription(
        factory, driver_id=driver_id, episode_id=episode_id,
        status="proposed",
    )
    p_accepted = await _add_prescription(
        factory, driver_id=driver_id, episode_id=episode_id,
        status="accepted",
    )
    token = _bearer(keys, "u_open", "open@test")
    resp = TestClient(app).get(
        f"/api/v1/driver/{driver_id}/prescriptions?status=open",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    ids = {item["id"] for item in body["items"]}
    assert ids == {str(p_proposed), str(p_accepted)}


async def test_list_open_excludes_skipped(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    _, driver_id, episode_id = await _seed_user_with_driver(
        factory, sub="u_skip", email="skip@test"
    )
    p_proposed = await _add_prescription(
        factory, driver_id=driver_id, episode_id=episode_id,
        status="proposed",
    )
    await _add_prescription(
        factory, driver_id=driver_id, episode_id=episode_id,
        status="skipped",
    )
    token = _bearer(keys, "u_skip", "skip@test")
    resp = TestClient(app).get(
        f"/api/v1/driver/{driver_id}/prescriptions?status=open",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == str(p_proposed)


async def test_list_default_status_is_open(
    configured_app: dict[str, Any],
) -> None:
    """Omitting ?status defaults to 'open' — the home-screen call."""
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    _, driver_id, episode_id = await _seed_user_with_driver(
        factory, sub="u_default", email="default@test"
    )
    await _add_prescription(
        factory, driver_id=driver_id, episode_id=episode_id,
        status="proposed",
    )
    await _add_prescription(
        factory, driver_id=driver_id, episode_id=episode_id,
        status="completed",
    )
    token = _bearer(keys, "u_default", "default@test")
    resp = TestClient(app).get(
        f"/api/v1/driver/{driver_id}/prescriptions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "proposed"


async def test_list_item_includes_required_fields(
    configured_app: dict[str, Any],
) -> None:
    """List item carries enough for the UI card without a follow-up
    fetch: title, drill_template_id, success_condition, status."""
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    _, driver_id, episode_id = await _seed_user_with_driver(
        factory, sub="u_fields", email="fields@test"
    )
    await _add_prescription(
        factory, driver_id=driver_id, episode_id=episode_id,
        status="proposed",
    )
    token = _bearer(keys, "u_fields", "fields@test")
    resp = TestClient(app).get(
        f"/api/v1/driver/{driver_id}/prescriptions?status=open",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["title"] == "Address late braking"
    assert item["drill_template_id"] == "drill.x"
    assert item["success_condition"]["metric"] == "apex_speed"
    assert item["status"] == "proposed"
    assert item["priority"] == "p1"
