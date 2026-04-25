import base64
import time
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from alembic.config import Config as AlembicConfig
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from alembic import command
from app.deps import get_db, get_jwks_client, get_pairing_store
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.pairing_store import PairingStore

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
    store = PairingStore()

    async def fake_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    def fake_jwks_client() -> ClerkJwksClient:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=keys["jwks"])

        return ClerkJwksClient(
            "https://test.clerk", transport=httpx.MockTransport(handler)
        )

    def fake_pairing_store() -> PairingStore:
        return store

    app.dependency_overrides[get_db] = fake_get_db
    app.dependency_overrides[get_jwks_client] = fake_jwks_client
    app.dependency_overrides[get_pairing_store] = fake_pairing_store
    try:
        yield {"keys": keys, "store": store}
    finally:
        app.dependency_overrides.clear()


def _token(keys: dict[str, Any], sub: str, email: str) -> str:
    now = int(time.time())
    return _sign(
        keys["priv_pem"],
        {"sub": sub, "email": email, "exp": now + 3600},
        keys["kid"],
    )


def test_create_pairing_returns_200_with_token(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    token = _token(keys, "user_pair_a", "a@test")
    client = TestClient(app)
    resp = client.post(
        "/api/v1/auth/pairing/create",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "pairing_token" in body
    assert isinstance(body["pairing_token"], str)
    assert len(body["pairing_token"]) >= 32


@pytest.mark.usefixtures("configured_app")
def test_create_pairing_without_auth_returns_401() -> None:
    client = TestClient(app)
    resp = client.post("/api/v1/auth/pairing/create")
    assert resp.status_code == 401


def test_create_pairing_returns_distinct_tokens(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    token = _token(keys, "user_pair_b", "b@test")
    client = TestClient(app)
    r1 = client.post(
        "/api/v1/auth/pairing/create",
        headers={"Authorization": f"Bearer {token}"},
    )
    r2 = client.post(
        "/api/v1/auth/pairing/create",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["pairing_token"] != r2.json()["pairing_token"]


def test_create_pairing_persists_token_to_store(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    store: PairingStore = configured_app["store"]
    token = _token(keys, "user_pair_c", "c@test")
    client = TestClient(app)
    resp = client.post(
        "/api/v1/auth/pairing/create",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    pairing_token = resp.json()["pairing_token"]
    user_id = store.consume(pairing_token)
    assert user_id is not None


def test_create_pairing_token_associates_to_authenticated_user(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    store: PairingStore = configured_app["store"]

    t1 = _token(keys, "user_alpha", "alpha@test")
    t2 = _token(keys, "user_beta", "beta@test")

    client = TestClient(app)
    r1 = client.post(
        "/api/v1/auth/pairing/create",
        headers={"Authorization": f"Bearer {t1}"},
    )
    r2 = client.post(
        "/api/v1/auth/pairing/create",
        headers={"Authorization": f"Bearer {t2}"},
    )
    pt1 = r1.json()["pairing_token"]
    pt2 = r2.json()["pairing_token"]
    user1 = store.consume(pt1)
    user2 = store.consume(pt2)
    assert user1 is not None
    assert user2 is not None
    assert user1 != user2
