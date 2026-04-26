"""T216 — `POST /api/v1/prescriptions/:id/skip`.

Symmetric to T215's accept endpoint. The driver POSTs and the
server flips `action_prescriptions.status` from 'proposed' to
'skipped'. Asymmetric to accept on purpose: no `skipped_at`
column — there is no outcome to time and nothing to follow up
on, so a single status flip captures the whole intent.

Auth posture mirrors `sessions.get_session` and T215: 404 for
both unknown and not-owned, so prescription IDs aren't leakable
across users. Idempotent — re-skipping a skipped prescription
is a 200 no-op.

Note: T216 does NOT prevent skip-after-accept here; the lifecycle
state-machine guard is a future task (e.g. once the UI surfaces
both buttons in conflict). For now, skip is a terminal write.
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


async def _seed_user_with_prescription(
    factory: Any, *, sub: str, email: str
) -> tuple[UUID, UUID]:
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    episode_id = uuid6.uuid7()
    prescription_id = uuid6.uuid7()
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
        db.add(
            ActionPrescription(
                id=prescription_id, driver_id=driver_id,
                weakness_episode_id=episode_id, created_at=now,
                priority="p1", title="Address late braking",
                rationale="r", expected_context={},
                success_condition={}, status="proposed",
                drill_template_id="drill.x",
            )
        )
        await db.commit()
    return user_id, prescription_id


async def _read_prescription(
    factory: Any, prescription_id: UUID
) -> ActionPrescription:
    async with factory() as db:
        return (
            await db.execute(
                select(ActionPrescription).where(
                    ActionPrescription.id == prescription_id
                )
            )
        ).scalar_one()


def test_skip_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    fake_id = uuid6.uuid7()
    resp = TestClient(app).post(
        f"/api/v1/prescriptions/{fake_id}/skip"
    )
    assert resp.status_code == 401


async def test_skip_unknown_prescription_returns_404(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    async with factory() as db:
        db.add(
            User(
                id=uuid6.uuid7(),
                clerk_user_id="u_no_pres_skip",
                email="nps@test",
            )
        )
        await db.commit()
    token = _bearer(keys, "u_no_pres_skip", "nps@test")
    fake_id = uuid6.uuid7()
    resp = TestClient(app).post(
        f"/api/v1/prescriptions/{fake_id}/skip",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


async def test_skip_prescription_owned_by_other_user_returns_404(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    _, prescription_id = await _seed_user_with_prescription(
        factory, sub="u_owner_oth_s", email="owner_oth_s@test"
    )
    async with factory() as db:
        db.add(
            User(
                id=uuid6.uuid7(),
                clerk_user_id="u_intruder_s",
                email="intruder_s@test",
            )
        )
        await db.commit()
    token = _bearer(keys, "u_intruder_s", "intruder_s@test")
    resp = TestClient(app).post(
        f"/api/v1/prescriptions/{prescription_id}/skip",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
    row = await _read_prescription(factory, prescription_id)
    assert row.status == "proposed"


async def test_skip_flips_status_to_skipped(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    _, prescription_id = await _seed_user_with_prescription(
        factory, sub="u_owner_s", email="owner_s@test"
    )
    token = _bearer(keys, "u_owner_s", "owner_s@test")
    resp = TestClient(app).post(
        f"/api/v1/prescriptions/{prescription_id}/skip",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    row = await _read_prescription(factory, prescription_id)
    assert row.status == "skipped"


async def test_skip_does_not_set_accepted_at(
    configured_app: dict[str, Any],
) -> None:
    """Asymmetric to accept — accepted_at stays None."""
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    _, prescription_id = await _seed_user_with_prescription(
        factory, sub="u_owner_aa", email="owner_aa@test"
    )
    token = _bearer(keys, "u_owner_aa", "owner_aa@test")
    resp = TestClient(app).post(
        f"/api/v1/prescriptions/{prescription_id}/skip",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    row = await _read_prescription(factory, prescription_id)
    assert row.accepted_at is None


async def test_skip_returns_status_in_body(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    _, prescription_id = await _seed_user_with_prescription(
        factory, sub="u_owner_body_s", email="owner_body_s@test"
    )
    token = _bearer(keys, "u_owner_body_s", "owner_body_s@test")
    resp = TestClient(app).post(
        f"/api/v1/prescriptions/{prescription_id}/skip",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(prescription_id)
    assert body["status"] == "skipped"


async def test_skip_is_idempotent(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    _, prescription_id = await _seed_user_with_prescription(
        factory, sub="u_owner_idem_s", email="owner_idem_s@test"
    )
    token = _bearer(keys, "u_owner_idem_s", "owner_idem_s@test")
    headers = {"Authorization": f"Bearer {token}"}
    url = f"/api/v1/prescriptions/{prescription_id}/skip"
    r1 = TestClient(app).post(url, headers=headers)
    r2 = TestClient(app).post(url, headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 200
    row = await _read_prescription(factory, prescription_id)
    assert row.status == "skipped"
