"""T053 (sprint2-backend) — PATCH /api/v1/driver/profile.

Lets the authenticated driver toggle cohort_opt_in (and update
the same fields PATCH /me supports). The privacy-toggle is the
T053 focus, but accepting the rest of DriverProfileUpdate keeps
the endpoint a useful general-purpose profile patcher and lets
the UI hit one endpoint regardless of which field changed.

Cross-driver isolation: only the authed user's own profile.
Missing profile → 404 (separate-create flow lives on PATCH /me).
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import AsyncGenerator, Generator
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
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, User

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


async def _seed_driver(
    factory: Any, *, clerk_user_id: str, cohort_opt_in: bool = False
) -> dict[str, Any]:
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    async with factory() as db:
        db.add(
            User(
                id=user_id, clerk_user_id=clerk_user_id,
                email=f"{clerk_user_id}@t",
            )
        )
        db.add(
            DriverProfile(
                id=driver_id, user_id=user_id, display_name="Driver",
                skill_level="beginner", cohort_opt_in=cohort_opt_in,
            )
        )
        await db.commit()
    return {"user_id": user_id, "driver_id": driver_id}


def _patch(
    client: TestClient, ca: dict[str, Any], who: str,
    body: dict[str, Any],
) -> Any:
    return client.patch(
        "/api/v1/driver/profile",
        headers={"Authorization": f"Bearer {_bearer(ca['keys'], who)}"},
        json=body,
    )


# -- happy path ---------------------------------------------------


def test_toggle_cohort_opt_in_to_true_persists(
    configured_app: dict[str, Any],
) -> None:
    """T053 acceptance: a driver with default cohort_opt_in=False
    can flip it to True via PATCH; the change is persisted."""
    seed = asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="alice")
    )
    with TestClient(app) as client:
        resp = _patch(
            client, configured_app, "alice", {"cohort_opt_in": True}
        )
    assert resp.status_code == 200
    assert resp.json()["cohort_opt_in"] is True

    async def reread() -> bool:
        async with configured_app["factory"]() as db:
            row = (
                await db.execute(
                    select(DriverProfile).where(
                        DriverProfile.id == seed["driver_id"]
                    )
                )
            ).scalar_one()
            return row.cohort_opt_in

    assert asyncio.run(reread()) is True


def test_toggle_cohort_opt_in_back_to_false(
    configured_app: dict[str, Any],
) -> None:
    """Re-toggling: True → False also lands. Symmetric path."""
    asyncio.run(
        _seed_driver(
            configured_app["factory"], clerk_user_id="alice",
            cohort_opt_in=True,
        )
    )
    with TestClient(app) as client:
        resp = _patch(
            client, configured_app, "alice", {"cohort_opt_in": False}
        )
    assert resp.status_code == 200
    assert resp.json()["cohort_opt_in"] is False


def test_unspecified_field_left_unchanged(
    configured_app: dict[str, Any],
) -> None:
    """PATCH semantics: a field not in the body is not touched.
    Toggling cohort_opt_in must not reset display_name or skill_level."""
    asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="alice")
    )
    with TestClient(app) as client:
        resp = _patch(
            client, configured_app, "alice", {"cohort_opt_in": True}
        )
    body = resp.json()
    assert body["display_name"] == "Driver"
    assert body["skill_level"] == "beginner"


def test_display_name_can_also_be_updated(
    configured_app: dict[str, Any],
) -> None:
    """The endpoint accepts the full DriverProfileUpdate body —
    not just the cohort flag — so the UI can hit one endpoint
    regardless of which field changed."""
    asyncio.run(
        _seed_driver(configured_app["factory"], clerk_user_id="alice")
    )
    with TestClient(app) as client:
        resp = _patch(
            client, configured_app, "alice",
            {"display_name": "New Name", "cohort_opt_in": True},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["display_name"] == "New Name"
    assert body["cohort_opt_in"] is True


# -- isolation ----------------------------------------------------


def test_no_profile_returns_404(
    configured_app: dict[str, Any],
) -> None:
    """User with no DriverProfile row → 404. Profile creation
    happens via PATCH /me's bootstrap path; this endpoint is
    update-only."""
    user_id = uuid6.uuid7()
    factory = configured_app["factory"]

    async def seed_user_only() -> None:
        async with factory() as db:
            db.add(
                User(id=user_id, clerk_user_id="alice", email="a@t")
            )
            await db.commit()

    asyncio.run(seed_user_only())
    with TestClient(app) as client:
        resp = _patch(
            client, configured_app, "alice", {"cohort_opt_in": True}
        )
    assert resp.status_code == 404


def test_cross_driver_isolation(
    configured_app: dict[str, Any],
) -> None:
    """Each user only patches their own profile. Bob's PATCH
    affects only bob's row, never alice's."""
    asyncio.run(
        _seed_driver(
            configured_app["factory"], clerk_user_id="alice",
            cohort_opt_in=False,
        )
    )
    bob_seed = asyncio.run(
        _seed_driver(
            configured_app["factory"], clerk_user_id="bob",
            cohort_opt_in=False,
        )
    )
    with TestClient(app) as client:
        _patch(client, configured_app, "bob", {"cohort_opt_in": True})

    async def alice_state() -> bool:
        async with configured_app["factory"]() as db:
            rows = (
                await db.execute(
                    select(DriverProfile).where(
                        DriverProfile.id != bob_seed["driver_id"]
                    )
                )
            ).scalars().all()
            return rows[0].cohort_opt_in

    assert asyncio.run(alice_state()) is False
