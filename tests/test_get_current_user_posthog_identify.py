"""T174 — `get_current_user` calls PostHog `set` (the 7.x identify
equivalent) on a user's first auth call, when the User row is
auto-provisioned. Subsequent calls with the same Clerk subject
must NOT re-fire — that would inflate the unique-user count.
"""

import base64
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from alembic.config import Config as AlembicConfig
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from alembic import command
from app.deps import (
    get_current_user,
    get_db,
    get_jwks_client,
    get_posthog_client,
)
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import User

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


class RecordingPosthog:
    def __init__(self) -> None:
        self.set_calls: list[dict[str, Any]] = []
        self.capture_calls: list[tuple[str, dict[str, Any]]] = []

    def capture(self, event: str, **kwargs: Any) -> None:
        self.capture_calls.append((event, kwargs))

    def set(self, **kwargs: Any) -> None:
        self.set_calls.append(kwargs)


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
def upgraded_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


def _make_app(
    db_url: str,
    jwks_payload: dict[str, Any],
    posthog: RecordingPosthog,
) -> FastAPI:
    app = FastAPI()
    engine = make_engine(db_url)
    factory = make_session_factory(engine)

    async def fake_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    def fake_jwks_client() -> ClerkJwksClient:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=jwks_payload)

        return ClerkJwksClient(
            "https://test.clerk", transport=httpx.MockTransport(handler)
        )

    app.dependency_overrides[get_db] = fake_get_db
    app.dependency_overrides[get_jwks_client] = fake_jwks_client
    app.dependency_overrides[get_posthog_client] = lambda: posthog

    @app.get("/me")
    async def me(
        user: User = Depends(get_current_user),  # noqa: B008
    ) -> dict[str, str]:
        return {"id": str(user.id), "email": user.email}

    return app


def test_first_login_fires_posthog_set(upgraded_db_url: str) -> None:
    keys = _build_keypair()
    posthog = RecordingPosthog()
    token = _sign(
        keys["priv_pem"],
        {
            "sub": "user_first_t174",
            "email": "first@test",
            "exp": int(time.time()) + 3600,
        },
        keys["kid"],
    )
    client = TestClient(_make_app(upgraded_db_url, keys["jwks"], posthog))
    resp = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200

    assert len(posthog.set_calls) == 1
    call = posthog.set_calls[0]
    assert call["distinct_id"] == resp.json()["id"]
    props = call["properties"]
    assert props["email"] == "first@test"
    assert props["clerk_user_id"] == "user_first_t174"


def test_second_login_does_not_re_identify(upgraded_db_url: str) -> None:
    keys = _build_keypair()
    posthog = RecordingPosthog()
    token = _sign(
        keys["priv_pem"],
        {
            "sub": "user_repeat_t174",
            "email": "repeat@test",
            "exp": int(time.time()) + 3600,
        },
        keys["kid"],
    )
    client = TestClient(_make_app(upgraded_db_url, keys["jwks"], posthog))
    client.get("/me", headers={"Authorization": f"Bearer {token}"})
    client.get("/me", headers={"Authorization": f"Bearer {token}"})

    # First call provisions and identifies; second call short-circuits
    # to the existing row and must skip the identify hop.
    assert len(posthog.set_calls) == 1


def test_different_users_produce_separate_identifies(
    upgraded_db_url: str,
) -> None:
    keys = _build_keypair()
    posthog = RecordingPosthog()
    t1 = _sign(
        keys["priv_pem"],
        {
            "sub": "user_alpha_t174",
            "email": "a@test",
            "exp": int(time.time()) + 3600,
        },
        keys["kid"],
    )
    t2 = _sign(
        keys["priv_pem"],
        {
            "sub": "user_beta_t174",
            "email": "b@test",
            "exp": int(time.time()) + 3600,
        },
        keys["kid"],
    )
    client = TestClient(_make_app(upgraded_db_url, keys["jwks"], posthog))
    client.get("/me", headers={"Authorization": f"Bearer {t1}"})
    client.get("/me", headers={"Authorization": f"Bearer {t2}"})

    assert len(posthog.set_calls) == 2
    distinct_ids = {c["distinct_id"] for c in posthog.set_calls}
    assert len(distinct_ids) == 2
