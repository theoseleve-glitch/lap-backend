import base64
import time
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any
from uuid import UUID

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
from infra.pairing_store import RedisPairingStore

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
    import fakeredis

    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")

    keys = _build_keypair()
    engine = make_engine(url)
    factory = make_session_factory(engine)
    redis = fakeredis.FakeRedis()
    store = RedisPairingStore(redis, ttl_seconds=300)

    async def fake_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    def fake_jwks_client() -> ClerkJwksClient:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=keys["jwks"])

        return ClerkJwksClient(
            "https://test.clerk", transport=httpx.MockTransport(handler)
        )

    def fake_pairing_store() -> RedisPairingStore:
        return store

    app.dependency_overrides[get_db] = fake_get_db
    app.dependency_overrides[get_jwks_client] = fake_jwks_client
    app.dependency_overrides[get_pairing_store] = fake_pairing_store
    try:
        yield {"keys": keys, "redis": redis, "store": store}
    finally:
        app.dependency_overrides.clear()


def _user_token(keys: dict[str, Any], sub: str, email: str) -> str:
    now = int(time.time())
    return _sign(
        keys["priv_pem"],
        {"sub": sub, "email": email, "exp": now + 3600},
        keys["kid"],
    )


def test_create_then_redeem_round_trip_against_redis(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    redis = configured_app["redis"]
    bearer = _user_token(keys, "user_pair_redis", "rrr@test")
    client = TestClient(app)

    create = client.post(
        "/api/v1/auth/pairing/create",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    assert create.status_code == 200
    pairing_token = create.json()["pairing_token"]

    # Confirm the token landed in Redis under the expected namespace with TTL.
    ttl = redis.ttl(f"pairing:{pairing_token}")
    assert 290 <= ttl <= 300

    redeem = client.post(
        "/api/v1/auth/pairing/redeem",
        json={"pairing_token": pairing_token},
    )
    assert redeem.status_code == 200
    assert "refresh_token" in redeem.json()

    # After redeem the key must be gone (single-use).
    assert redis.get(f"pairing:{pairing_token}") is None


def test_redis_store_redeem_is_single_use_via_route(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    bearer = _user_token(keys, "user_single", "single@test")
    client = TestClient(app)

    create = client.post(
        "/api/v1/auth/pairing/create",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    pairing_token = create.json()["pairing_token"]

    r1 = client.post(
        "/api/v1/auth/pairing/redeem", json={"pairing_token": pairing_token}
    )
    r2 = client.post(
        "/api/v1/auth/pairing/redeem", json={"pairing_token": pairing_token}
    )
    assert r1.status_code == 200
    assert r2.status_code == 401


def test_redis_store_redeem_after_expiry_returns_401(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    redis = configured_app["redis"]
    bearer = _user_token(keys, "user_expired", "exp@test")
    client = TestClient(app)

    create = client.post(
        "/api/v1/auth/pairing/create",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    pairing_token = create.json()["pairing_token"]

    # Simulate the 5-minute TTL elapsing by deleting the key directly.
    redis.delete(f"pairing:{pairing_token}")

    redeem = client.post(
        "/api/v1/auth/pairing/redeem",
        json={"pairing_token": pairing_token},
    )
    assert redeem.status_code == 401


def test_redis_store_create_associates_token_with_authenticated_user(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    store: RedisPairingStore = configured_app["store"]
    bearer = _user_token(keys, "user_assoc", "assoc@test")
    client = TestClient(app)

    create = client.post(
        "/api/v1/auth/pairing/create",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    pairing_token = create.json()["pairing_token"]
    user_id = store.consume(pairing_token)
    assert isinstance(user_id, UUID)
