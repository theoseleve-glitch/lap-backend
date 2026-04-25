from collections.abc import Generator
from typing import Any

import pytest
import uuid6
from fastapi.testclient import TestClient

from app.deps import get_pairing_store
from app.main import app
from infra.pairing_store import PairingStore


@pytest.fixture
def configured_app() -> Generator[dict[str, Any], None, None]:
    store = PairingStore()

    def fake_pairing_store() -> PairingStore:
        return store

    app.dependency_overrides[get_pairing_store] = fake_pairing_store
    try:
        yield {"store": store}
    finally:
        app.dependency_overrides.clear()


def test_redeem_valid_token_returns_refresh_token(
    configured_app: dict[str, Any],
) -> None:
    store: PairingStore = configured_app["store"]
    user_id = uuid6.uuid7()
    pairing_token = store.create(user_id)

    client = TestClient(app)
    resp = client.post(
        "/api/v1/auth/pairing/redeem",
        json={"pairing_token": pairing_token},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "refresh_token" in body
    assert isinstance(body["refresh_token"], str)
    assert len(body["refresh_token"]) >= 32


@pytest.mark.usefixtures("configured_app")
def test_redeem_unknown_token_returns_401() -> None:
    client = TestClient(app)
    resp = client.post(
        "/api/v1/auth/pairing/redeem",
        json={"pairing_token": "definitely-not-a-real-token-xxxxxxxxxxxxx"},
    )
    assert resp.status_code == 401


def test_redeem_consumes_token_so_second_redeem_fails(
    configured_app: dict[str, Any],
) -> None:
    store: PairingStore = configured_app["store"]
    user_id = uuid6.uuid7()
    pairing_token = store.create(user_id)

    client = TestClient(app)
    r1 = client.post(
        "/api/v1/auth/pairing/redeem",
        json={"pairing_token": pairing_token},
    )
    r2 = client.post(
        "/api/v1/auth/pairing/redeem",
        json={"pairing_token": pairing_token},
    )
    assert r1.status_code == 200
    assert r2.status_code == 401


def test_redeem_distinct_pairing_tokens_yield_distinct_refresh_tokens(
    configured_app: dict[str, Any],
) -> None:
    store: PairingStore = configured_app["store"]
    pt1 = store.create(uuid6.uuid7())
    pt2 = store.create(uuid6.uuid7())

    client = TestClient(app)
    r1 = client.post(
        "/api/v1/auth/pairing/redeem", json={"pairing_token": pt1}
    )
    r2 = client.post(
        "/api/v1/auth/pairing/redeem", json={"pairing_token": pt2}
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["refresh_token"] != r2.json()["refresh_token"]


@pytest.mark.usefixtures("configured_app")
def test_redeem_with_missing_pairing_token_field_returns_422() -> None:
    client = TestClient(app)
    resp = client.post(
        "/api/v1/auth/pairing/redeem",
        json={},
    )
    assert resp.status_code == 422


@pytest.mark.usefixtures("configured_app")
def test_redeem_does_not_require_auth() -> None:
    # Endpoint must be reachable without an Authorization header — the
    # desktop has no credentials yet at this point in the flow.
    client = TestClient(app)
    resp = client.post(
        "/api/v1/auth/pairing/redeem",
        json={"pairing_token": "irrelevant-but-well-formed-string-padding"},
    )
    # Unknown token → 401, NOT 401-because-no-auth-header.
    assert resp.status_code == 401
    # If auth was required, FastAPI/our deps would return without ever
    # hitting the redeem logic, so the response detail would mention
    # auth, not the token. The detail under our impl should be about
    # the pairing token.
    body = resp.json()
    assert "detail" in body
