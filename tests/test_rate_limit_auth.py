from collections.abc import Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.deps import get_pairing_store
from app.main import app
from app.rate_limit import reset_rate_limiter, set_rate_limiter
from infra.pairing_store import PairingStore
from infra.rate_limiter import RateLimiter


@pytest.fixture
def configured_app() -> Generator[dict[str, Any], None, None]:
    import fakeredis

    redis_client = fakeredis.FakeRedis()
    limiter = RateLimiter(redis_client, limit=10, window_seconds=60)
    set_rate_limiter(limiter)

    # /pairing/redeem only needs the pairing store; bypass real Redis there.
    app.dependency_overrides[get_pairing_store] = lambda: PairingStore()

    try:
        yield {"redis": redis_client, "limiter": limiter}
    finally:
        reset_rate_limiter()
        app.dependency_overrides.clear()


def _redeem_payload() -> dict[str, str]:
    # 32 chars (passes lap-schemas length validation but never matches a
    # real stored token, so the handler returns 401). The status doesn't
    # matter for rate-limit testing — just that the request reached the
    # middleware.
    return {"pairing_token": "x" * 32}


@pytest.mark.usefixtures("configured_app")
def test_eleventh_auth_request_from_same_ip_returns_429() -> None:
    client = TestClient(app)
    payload = _redeem_payload()

    for _ in range(10):
        resp = client.post("/api/v1/auth/pairing/redeem", json=payload)
        assert resp.status_code != 429

    resp = client.post("/api/v1/auth/pairing/redeem", json=payload)
    assert resp.status_code == 429


@pytest.mark.usefixtures("configured_app")
def test_429_response_body_includes_detail() -> None:
    client = TestClient(app)
    payload = _redeem_payload()

    for _ in range(10):
        client.post("/api/v1/auth/pairing/redeem", json=payload)
    resp = client.post("/api/v1/auth/pairing/redeem", json=payload)

    assert resp.status_code == 429
    body = resp.json()
    assert "detail" in body


@pytest.mark.usefixtures("configured_app")
def test_non_auth_path_is_not_rate_limited() -> None:
    client = TestClient(app)

    for _ in range(20):
        resp = client.get("/healthz")
        assert resp.status_code == 200


@pytest.mark.usefixtures("configured_app")
def test_different_ips_get_separate_buckets() -> None:
    client = TestClient(app)
    payload = _redeem_payload()

    # IP A burns 10 requests.
    for _ in range(10):
        client.post(
            "/api/v1/auth/pairing/redeem",
            json=payload,
            headers={"X-Forwarded-For": "10.0.0.1"},
        )

    # IP A's 11th request is blocked.
    blocked = client.post(
        "/api/v1/auth/pairing/redeem",
        json=payload,
        headers={"X-Forwarded-For": "10.0.0.1"},
    )
    assert blocked.status_code == 429

    # IP B's first request is allowed.
    fresh = client.post(
        "/api/v1/auth/pairing/redeem",
        json=payload,
        headers={"X-Forwarded-For": "10.0.0.2"},
    )
    assert fresh.status_code != 429


@pytest.mark.usefixtures("configured_app")
def test_within_limit_requests_pass_through() -> None:
    client = TestClient(app)
    payload = _redeem_payload()

    statuses = [
        client.post("/api/v1/auth/pairing/redeem", json=payload).status_code
        for _ in range(10)
    ]
    assert all(s != 429 for s in statuses)


def test_rate_limiter_not_configured_means_no_enforcement() -> None:
    # When set_rate_limiter is never called, the middleware passes through.
    # This test deliberately runs WITHOUT the configured_app fixture to
    # confirm the bypass behavior — required so that existing tests
    # (which don't set up a limiter) keep working unchanged.
    reset_rate_limiter()
    app.dependency_overrides[get_pairing_store] = lambda: PairingStore()
    try:
        client = TestClient(app)
        payload = _redeem_payload()
        for _ in range(15):
            resp = client.post("/api/v1/auth/pairing/redeem", json=payload)
            assert resp.status_code != 429
    finally:
        app.dependency_overrides.clear()
