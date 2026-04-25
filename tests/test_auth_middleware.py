import base64
import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.deps import get_current_user, get_jwks_client
from infra.clerk import ClerkJwksClient


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _build_keypair(kid: str = "test-kid-1") -> dict[str, Any]:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_numbers = priv.public_key().public_numbers()
    n_bytes = pub_numbers.n.to_bytes(
        (pub_numbers.n.bit_length() + 7) // 8, "big"
    )
    e_bytes = pub_numbers.e.to_bytes(
        (pub_numbers.e.bit_length() + 7) // 8, "big"
    )
    jwk = {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": _b64u(n_bytes),
        "e": _b64u(e_bytes),
    }
    return {"priv_pem": priv_pem, "kid": kid, "jwks": {"keys": [jwk]}}


def _sign(priv_pem: bytes, claims: dict[str, Any], kid: str) -> str:
    return jwt.encode(
        claims, priv_pem, algorithm="RS256", headers={"kid": kid}
    )


def _make_app(jwks_payload: dict[str, Any]) -> FastAPI:
    app = FastAPI()

    def fake_jwks_client() -> ClerkJwksClient:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=jwks_payload)

        return ClerkJwksClient(
            "https://test.clerk", transport=httpx.MockTransport(handler)
        )

    app.dependency_overrides[get_jwks_client] = fake_jwks_client

    @app.get("/me")
    async def me(
        user: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    ) -> dict[str, Any]:
        return user

    return app


@pytest.fixture
def keys() -> dict[str, Any]:
    return _build_keypair()


def test_valid_jwt_returns_claims(keys: dict[str, Any]) -> None:
    now = int(time.time())
    token = _sign(
        keys["priv_pem"],
        {"sub": "user_abc", "exp": now + 3600, "iat": now},
        keys["kid"],
    )
    client = TestClient(_make_app(keys["jwks"]))
    resp = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["sub"] == "user_abc"


def test_missing_authorization_header_returns_401(
    keys: dict[str, Any],
) -> None:
    client = TestClient(_make_app(keys["jwks"]))
    resp = client.get("/me")
    assert resp.status_code == 401


def test_non_bearer_scheme_returns_401(keys: dict[str, Any]) -> None:
    client = TestClient(_make_app(keys["jwks"]))
    resp = client.get("/me", headers={"Authorization": "Basic abc"})
    assert resp.status_code == 401


def test_malformed_token_returns_401(keys: dict[str, Any]) -> None:
    client = TestClient(_make_app(keys["jwks"]))
    resp = client.get(
        "/me", headers={"Authorization": "Bearer not.a.real.jwt"}
    )
    assert resp.status_code == 401


def test_invalid_signature_returns_401(keys: dict[str, Any]) -> None:
    other = _build_keypair(kid="test-kid-1")
    now = int(time.time())
    bad = _sign(
        other["priv_pem"],
        {"sub": "user_x", "exp": now + 3600},
        keys["kid"],
    )
    client = TestClient(_make_app(keys["jwks"]))
    resp = client.get("/me", headers={"Authorization": f"Bearer {bad}"})
    assert resp.status_code == 401


def test_expired_token_returns_401(keys: dict[str, Any]) -> None:
    now = int(time.time())
    expired = _sign(
        keys["priv_pem"],
        {"sub": "user_x", "exp": now - 60, "iat": now - 3660},
        keys["kid"],
    )
    client = TestClient(_make_app(keys["jwks"]))
    resp = client.get(
        "/me", headers={"Authorization": f"Bearer {expired}"}
    )
    assert resp.status_code == 401


def test_unknown_kid_returns_401(keys: dict[str, Any]) -> None:
    now = int(time.time())
    token = _sign(
        keys["priv_pem"],
        {"sub": "user_x", "exp": now + 3600},
        kid="unknown-kid",
    )
    client = TestClient(_make_app(keys["jwks"]))
    resp = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
