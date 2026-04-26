"""T008 (sprint2-backend) — POST /prescriptions/:id/flag-wrong.

Marks a prescription as a user-flagged false positive. Stored as
a `flagged_wrong_at` timestamp on the row (None = not flagged).

The flag is **orthogonal to lifecycle status**: a flagged
prescription can still be in any of proposed/accepted/in_progress/
completed/skipped/expired. The flag captures user feedback about
the suggestion's quality, not its current state.

T011 will aggregate these flags to decrement rule confidence;
T008 just records them.
"""

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
from sqlalchemy.ext.asyncio import AsyncSession

from alembic import command
from app.deps import get_db, get_jwks_client
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import ActionPrescription, DriverProfile, User

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

    async def fake_get_db() -> AsyncGenerator[AsyncSession, None]:
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


async def _seed_prescription(
    factory: Any,
    *,
    clerk_user_id: str,
    status: str = "proposed",
) -> dict[str, Any]:
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    pid = uuid6.uuid7()
    async with factory() as db:
        db.add(
            User(
                id=user_id,
                clerk_user_id=clerk_user_id,
                email=f"{clerk_user_id}@t",
            )
        )
        db.add(DriverProfile(id=driver_id, user_id=user_id, display_name="A"))
        db.add(
            ActionPrescription(
                id=pid,
                driver_id=driver_id,
                weakness_episode_id=None,
                created_at=datetime(2026, 4, 26, tzinfo=UTC),
                priority="p0",
                title="t",
                rationale="r",
                drill_template_id="drill.x.v1",
                expected_context={},
                success_condition={"comparison": "gte", "target_pct": 0.85},
                status=status,
            )
        )
        await db.commit()
    return {"prescription_id": pid, "driver_id": driver_id}


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


def _flag(client: TestClient, ca: dict[str, Any], who: str, pid: Any) -> Any:
    return client.post(
        f"/api/v1/prescriptions/{pid}/flag-wrong",
        headers={"Authorization": f"Bearer {_bearer(ca['keys'], who)}"},
    )


# -- behavior ------------------------------------------------------


def test_flag_wrong_sets_timestamp(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_prescription(
            configured_app["factory"], clerk_user_id="u_fw"
        )
    )
    client = TestClient(app)
    resp = _flag(client, configured_app, "u_fw", seeded["prescription_id"])
    assert resp.status_code == 200

    async def _read() -> Any:
        async with configured_app["factory"]() as db:
            row = (
                await db.execute(
                    select(ActionPrescription).where(
                        ActionPrescription.id == seeded["prescription_id"]
                    )
                )
            ).scalar_one()
            return row.flagged_wrong_at

    flagged_at = asyncio.run(_read())
    assert flagged_at is not None


def test_flag_wrong_does_not_change_status(
    configured_app: dict[str, Any],
) -> None:
    """Flag is orthogonal to lifecycle status — flagging a
    proposed prescription leaves it as proposed."""
    seeded = asyncio.run(
        _seed_prescription(
            configured_app["factory"], clerk_user_id="u_st", status="proposed"
        )
    )
    client = TestClient(app)
    _flag(client, configured_app, "u_st", seeded["prescription_id"])

    async def _status() -> str:
        async with configured_app["factory"]() as db:
            row = (
                await db.execute(
                    select(ActionPrescription).where(
                        ActionPrescription.id == seeded["prescription_id"]
                    )
                )
            ).scalar_one()
            return row.status

    assert asyncio.run(_status()) == "proposed"


def test_flag_wrong_is_idempotent(
    configured_app: dict[str, Any],
) -> None:
    """Calling flag-wrong twice keeps the FIRST timestamp —
    the user's feedback was recorded at first click, not last."""
    seeded = asyncio.run(
        _seed_prescription(
            configured_app["factory"], clerk_user_id="u_id"
        )
    )
    client = TestClient(app)
    _flag(client, configured_app, "u_id", seeded["prescription_id"])

    async def _flagged_at() -> Any:
        async with configured_app["factory"]() as db:
            row = (
                await db.execute(
                    select(ActionPrescription).where(
                        ActionPrescription.id == seeded["prescription_id"]
                    )
                )
            ).scalar_one()
            return row.flagged_wrong_at

    first = asyncio.run(_flagged_at())
    _flag(client, configured_app, "u_id", seeded["prescription_id"])
    second = asyncio.run(_flagged_at())
    assert first == second


def test_flag_wrong_works_on_completed_prescription(
    configured_app: dict[str, Any],
) -> None:
    """Even after the prescription is done, the user can flag it
    in retrospect — useful for "this drill didn't actually help"."""
    seeded = asyncio.run(
        _seed_prescription(
            configured_app["factory"],
            clerk_user_id="u_done",
            status="completed",
        )
    )
    client = TestClient(app)
    resp = _flag(client, configured_app, "u_done", seeded["prescription_id"])
    assert resp.status_code == 200


def test_flag_wrong_other_drivers_prescription_returns_404(
    configured_app: dict[str, Any],
) -> None:
    asyncio.run(
        _seed_prescription(configured_app["factory"], clerk_user_id="u_a")
    )
    other = asyncio.run(
        _seed_prescription(configured_app["factory"], clerk_user_id="u_b")
    )
    client = TestClient(app)
    resp = _flag(client, configured_app, "u_a", other["prescription_id"])
    assert resp.status_code == 404


@pytest.mark.usefixtures("configured_app")
def test_flag_wrong_unauthenticated_returns_401() -> None:
    client = TestClient(app)
    resp = client.post(f"/api/v1/prescriptions/{uuid6.uuid7()}/flag-wrong")
    assert resp.status_code == 401


# -- existing endpoints still work (regression guard) -------------


def test_accept_still_transitions_status(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_prescription(
            configured_app["factory"], clerk_user_id="u_ac", status="proposed"
        )
    )
    client = TestClient(app)
    resp = client.post(
        f"/api/v1/prescriptions/{seeded['prescription_id']}/accept",
        headers={
            "Authorization": (
                f"Bearer {_bearer(configured_app['keys'], 'u_ac')}"
            )
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"


def test_skip_still_transitions_status(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed_prescription(
            configured_app["factory"], clerk_user_id="u_sk", status="proposed"
        )
    )
    client = TestClient(app)
    resp = client.post(
        f"/api/v1/prescriptions/{seeded['prescription_id']}/skip",
        headers={
            "Authorization": (
                f"Bearer {_bearer(configured_app['keys'], 'u_sk')}"
            )
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"
