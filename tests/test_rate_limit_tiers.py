"""T014 (sprint2-backend) — rate-limit enforcement tiers.

Three buckets:
- auth: 10/min/IP for /api/v1/auth/* (existing T-prior limit, retained)
- read: 100/min/user for the rest of /api/v1/* (T014 new)
- ingest: 10/hr/user for POST /api/v1/sessions/ingest (T014 new)

User identity for the read/ingest tiers is the Authorization header
value (cheap to extract; the full JWT verify happens in
`get_current_user` already). Falls back to IP when no auth header
is present so an anonymous flooder is still throttled.

Tests use small numbers (e.g. limit=3) for speed — the production
defaults (100, 10) are config concerns, not invariants.
"""

from collections.abc import Generator

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.deps import get_pairing_store
from app.main import app
from app.rate_limit import (
    reset_rate_limiter,
    set_ingest_rate_limiter,
    set_read_rate_limiter,
)
from infra.pairing_store import PairingStore
from infra.rate_limiter import RateLimiter


@pytest.fixture
def configured_read_limiter() -> Generator[RateLimiter, None, None]:
    """Install only a read-tier limiter so we can exercise GETs
    without crossing wires with auth or ingest."""
    redis_client = fakeredis.FakeRedis()
    limiter = RateLimiter(redis_client, limit=3, window_seconds=60)
    set_read_rate_limiter(limiter)
    app.dependency_overrides[get_pairing_store] = lambda: PairingStore()
    try:
        yield limiter
    finally:
        reset_rate_limiter()
        app.dependency_overrides.clear()


@pytest.fixture
def configured_ingest_limiter(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[RateLimiter, None, None]:
    # Ingest handler instantiates Config which requires these env
    # vars; we never reach the handler past the limit, but the
    # first allowed requests do.
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://x")
    monkeypatch.setenv("CLERK_SECRET", "sk_test_clerk")
    monkeypatch.setenv("STRIPE_SECRET", "sk_test_stripe")
    redis_client = fakeredis.FakeRedis()
    limiter = RateLimiter(redis_client, limit=2, window_seconds=3600)
    set_ingest_rate_limiter(limiter)
    app.dependency_overrides[get_pairing_store] = lambda: PairingStore()
    try:
        yield limiter
    finally:
        reset_rate_limiter()
        app.dependency_overrides.clear()


# -- read-tier behavior --------------------------------------------


@pytest.mark.usefixtures("configured_read_limiter")
def test_read_request_over_limit_returns_429() -> None:
    client = TestClient(app)
    headers = {"Authorization": "Bearer token-A"}
    for _ in range(3):
        resp = client.get("/api/v1/coaching/rules", headers=headers)
        assert resp.status_code != 429
    blocked = client.get("/api/v1/coaching/rules", headers=headers)
    assert blocked.status_code == 429


@pytest.mark.usefixtures("configured_read_limiter")
def test_read_429_body_includes_detail() -> None:
    client = TestClient(app)
    headers = {"Authorization": "Bearer token-A"}
    for _ in range(3):
        client.get("/api/v1/coaching/rules", headers=headers)
    blocked = client.get("/api/v1/coaching/rules", headers=headers)
    assert blocked.status_code == 429
    assert "detail" in blocked.json()


@pytest.mark.usefixtures("configured_read_limiter")
def test_different_users_get_separate_read_buckets() -> None:
    client = TestClient(app)
    for _ in range(3):
        client.get(
            "/api/v1/coaching/rules",
            headers={"Authorization": "Bearer token-A"},
        )
    blocked_a = client.get(
        "/api/v1/coaching/rules",
        headers={"Authorization": "Bearer token-A"},
    )
    fresh_b = client.get(
        "/api/v1/coaching/rules",
        headers={"Authorization": "Bearer token-B"},
    )
    assert blocked_a.status_code == 429
    assert fresh_b.status_code != 429


@pytest.mark.usefixtures("configured_read_limiter")
def test_read_limiter_does_not_apply_to_auth_paths() -> None:
    """auth/* has its own limiter; the read tier should not consume
    the auth bucket nor enforce on auth paths."""
    client = TestClient(app)
    payload = {"pairing_token": "x" * 32}
    for _ in range(20):  # well over read limit
        resp = client.post("/api/v1/auth/pairing/redeem", json=payload)
        assert resp.status_code != 429


@pytest.mark.usefixtures("configured_read_limiter")
def test_read_limiter_does_not_apply_to_health_endpoint() -> None:
    """Liveness/readiness probes must not be rate-limited or k8s
    will bounce healthy pods after a burst."""
    client = TestClient(app)
    for _ in range(10):
        resp = client.get("/healthz")
        assert resp.status_code == 200


# -- ingest-tier behavior ------------------------------------------


@pytest.mark.usefixtures("configured_ingest_limiter")
def test_ingest_request_over_limit_returns_429() -> None:
    """The exact ingest endpoint behavior (multipart, idempotency)
    is irrelevant — we only care that the rate-limit middleware
    fires before the handler is reached. So a malformed POST that
    would normally 422 still consumes the bucket."""
    client = TestClient(app)
    headers = {"Authorization": "Bearer token-A"}
    statuses = []
    for _ in range(2):
        resp = client.post(
            "/api/v1/sessions/ingest", headers=headers, content=b""
        )
        statuses.append(resp.status_code)
    blocked = client.post(
        "/api/v1/sessions/ingest", headers=headers, content=b""
    )
    assert all(s != 429 for s in statuses)
    assert blocked.status_code == 429


@pytest.mark.usefixtures("configured_ingest_limiter")
def test_different_users_get_separate_ingest_buckets() -> None:
    client = TestClient(app)
    for _ in range(2):
        client.post(
            "/api/v1/sessions/ingest",
            headers={"Authorization": "Bearer token-A"},
            content=b"",
        )
    blocked_a = client.post(
        "/api/v1/sessions/ingest",
        headers={"Authorization": "Bearer token-A"},
        content=b"",
    )
    fresh_b = client.post(
        "/api/v1/sessions/ingest",
        headers={"Authorization": "Bearer token-B"},
        content=b"",
    )
    assert blocked_a.status_code == 429
    assert fresh_b.status_code != 429


@pytest.mark.usefixtures("configured_ingest_limiter")
def test_ingest_limiter_does_not_apply_to_other_endpoints() -> None:
    """The ingest bucket only counts /sessions/ingest hits — read
    paths (without a read limiter installed) pass through freely."""
    client = TestClient(app)
    headers = {"Authorization": "Bearer token-A"}
    for _ in range(5):  # over the ingest limit (2)
        resp = client.get("/api/v1/coaching/rules", headers=headers)
        assert resp.status_code != 429


# -- bypass behavior -----------------------------------------------


def test_no_limiters_configured_passes_everything_through() -> None:
    """When no tier is installed (the default), the middleware must
    not enforce anything — preserves backward compat for existing
    test fixtures that never call any setter."""
    reset_rate_limiter()
    app.dependency_overrides[get_pairing_store] = lambda: PairingStore()
    try:
        client = TestClient(app)
        for _ in range(10):
            resp = client.get(
                "/api/v1/coaching/rules",
                headers={"Authorization": "Bearer token-A"},
            )
            assert resp.status_code != 429
    finally:
        app.dependency_overrides.clear()
