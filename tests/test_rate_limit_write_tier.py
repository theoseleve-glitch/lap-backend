"""T182 (sprint2-backend) — write-tier rate limiting.

Splits the existing two-tier scheme (read + ingest) into three so
state-mutating traffic (POST/PATCH/PUT/DELETE) gets its own bucket
distinct from chatty GETs:

  - read tier   : GETs on /api/v1/* (was: all methods on /api/v1/*)
  - write tier  : POST/PATCH/PUT/DELETE on /api/v1/* (NEW)
  - ingest tier : POST /api/v1/sessions/ingest (unchanged)
  - auth tier   : /api/v1/auth/* per-IP (unchanged)

Why split? A noisy UI background-poll loop on GET endpoints can
starve the same user's ability to actually save anything when
both share one bucket. Splitting by HTTP method matches the
operational reality: read traffic is high-frequency / low-cost,
write traffic is low-frequency / high-cost, and they should be
rate-limited independently.

Backward compatibility: if the write limiter is not installed
(via `set_write_rate_limiter`), POST/PATCH/PUT/DELETE on
/api/v1/* pass through unrate-limited. Existing fixtures that
only configure the read limiter keep working — they just stop
limiting writes (which they were never specifically testing).

Pinned behaviors:
  - 4 consecutive POSTs on /api/v1/coaching/insights/.../flag
    when write limiter has limit=3 → 4th returns 429
  - PATCH counts as a write (consumes the write bucket)
  - DELETE counts as a write
  - GET against /api/v1/* with ONLY a write limiter installed
    is NOT rate-limited (read tier is independent)
  - POST /api/v1/sessions/ingest is NOT consumed by the write
    bucket (uses ingest tier instead)
  - POST /api/v1/auth/* is NOT consumed by the write bucket
    (uses auth tier instead)
  - Read and write buckets are independent: fill the read bucket
    and writes still have full capacity
  - Without a write limiter, POSTs pass through (backward compat)
"""

from collections.abc import Generator

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.deps import get_pairing_store
from app.main import app
from app.rate_limit import (
    reset_rate_limiter,
    set_read_rate_limiter,
    set_write_rate_limiter,
)
from infra.pairing_store import PairingStore
from infra.rate_limiter import RateLimiter


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The handlers under POST/PATCH instantiate Config which needs
    these env vars; we never reach the handler past the limit, but
    the first allowed requests do."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://x")
    monkeypatch.setenv("CLERK_SECRET", "sk_test_clerk")
    monkeypatch.setenv("STRIPE_SECRET", "sk_test_stripe")


@pytest.fixture
def configured_write_limiter(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[RateLimiter, None, None]:
    """Install ONLY a write-tier limiter so writes are throttled
    but reads pass through (read limiter not installed)."""
    _set_required_env(monkeypatch)
    redis_client = fakeredis.FakeRedis()
    limiter = RateLimiter(redis_client, limit=3, window_seconds=60)
    set_write_rate_limiter(limiter)
    app.dependency_overrides[get_pairing_store] = lambda: PairingStore()
    try:
        yield limiter
    finally:
        reset_rate_limiter()
        app.dependency_overrides.clear()


@pytest.fixture
def configured_read_and_write_limiters(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[RateLimiter, RateLimiter], None, None]:
    _set_required_env(monkeypatch)
    redis_client = fakeredis.FakeRedis()
    read = RateLimiter(redis_client, limit=2, window_seconds=60)
    write = RateLimiter(redis_client, limit=2, window_seconds=60)
    set_read_rate_limiter(read)
    set_write_rate_limiter(write)
    app.dependency_overrides[get_pairing_store] = lambda: PairingStore()
    try:
        yield (read, write)
    finally:
        reset_rate_limiter()
        app.dependency_overrides.clear()


# -- write-tier basic enforcement ----------------------------------


@pytest.mark.usefixtures("configured_write_limiter")
def test_post_over_limit_returns_429() -> None:
    """4 consecutive POSTs against /api/v1/insights/.../flag with
    limit=3: the 4th must return 429. The handler will 401/422
    before the rate limiter — what matters is that the 4th's
    status flips to 429."""
    client = TestClient(app)
    headers = {"Authorization": "Bearer token-A"}
    statuses = []
    for _ in range(3):
        resp = client.post(
            "/api/v1/insights/00000000-0000-0000-0000-000000000000/flag",
            headers=headers,
        )
        statuses.append(resp.status_code)
    blocked = client.post(
        "/api/v1/insights/00000000-0000-0000-0000-000000000000/flag",
        headers=headers,
    )
    assert all(s != 429 for s in statuses), statuses
    assert blocked.status_code == 429


@pytest.mark.usefixtures("configured_write_limiter")
def test_patch_counts_as_write() -> None:
    """PATCH is a mutation — should consume the write bucket."""
    client = TestClient(app)
    headers = {"Authorization": "Bearer token-A"}
    for _ in range(3):
        client.patch(
            "/api/v1/me",
            headers=headers,
            json={"display_name": "x"},
        )
    blocked = client.patch(
        "/api/v1/me",
        headers=headers,
        json={"display_name": "x"},
    )
    assert blocked.status_code == 429


@pytest.mark.usefixtures("configured_write_limiter")
def test_delete_counts_as_write() -> None:
    """DELETE is a mutation — should consume the write bucket."""
    client = TestClient(app)
    headers = {"Authorization": "Bearer token-A"}
    target = "/api/v1/auth/devices/00000000-0000-0000-0000-000000000000"
    # /auth/devices/* is under /auth so it'd hit auth-tier — pick a
    # non-auth DELETE path instead. compare_cases is /api/v1/.
    target = (
        "/api/v1/compare_cases/00000000-0000-0000-0000-000000000000"
    )
    for _ in range(3):
        client.delete(target, headers=headers)
    blocked = client.delete(target, headers=headers)
    assert blocked.status_code == 429


# -- write-tier scope (does NOT apply to other methods/paths) ------


@pytest.mark.usefixtures("configured_write_limiter")
def test_get_not_rate_limited_by_write_tier() -> None:
    """GETs on /api/v1/* must NOT consume the write bucket. With
    only the write limiter installed (no read tier), GETs pass
    through freely no matter how many fire."""
    client = TestClient(app)
    headers = {"Authorization": "Bearer token-A"}
    for _ in range(10):  # well over write limit (3)
        resp = client.get("/api/v1/coaching/rules", headers=headers)
        assert resp.status_code != 429


@pytest.mark.usefixtures("configured_write_limiter")
def test_ingest_post_not_rate_limited_by_write_tier() -> None:
    """POST /api/v1/sessions/ingest uses the ingest tier, not
    write. Without an ingest limiter installed, those POSTs
    pass through even past the write limit."""
    client = TestClient(app)
    headers = {"Authorization": "Bearer token-A"}
    for _ in range(5):  # over write limit (3)
        resp = client.post(
            "/api/v1/sessions/ingest", headers=headers, content=b""
        )
        assert resp.status_code != 429


@pytest.mark.usefixtures("configured_write_limiter")
def test_auth_post_not_rate_limited_by_write_tier() -> None:
    """POST /api/v1/auth/* uses the auth tier (per-IP), not write.
    Without an auth limiter installed, those POSTs pass through
    even past the write limit."""
    client = TestClient(app)
    payload = {"pairing_token": "x" * 32}
    for _ in range(5):  # over write limit (3)
        resp = client.post(
            "/api/v1/auth/pairing/redeem", json=payload
        )
        assert resp.status_code != 429


# -- read and write buckets are independent ------------------------


@pytest.mark.usefixtures("configured_read_and_write_limiters")
def test_filling_read_bucket_does_not_block_writes() -> None:
    """User fills the read bucket browsing the UI — they should
    still be able to save state. The two buckets must be fully
    independent."""
    client = TestClient(app)
    headers = {"Authorization": "Bearer token-A"}
    # Fill read bucket (limit=2).
    for _ in range(2):
        resp = client.get("/api/v1/coaching/rules", headers=headers)
        assert resp.status_code != 429
    blocked_read = client.get("/api/v1/coaching/rules", headers=headers)
    assert blocked_read.status_code == 429

    # Write bucket should still have full capacity (limit=2).
    fresh_write = client.post(
        "/api/v1/insights/00000000-0000-0000-0000-000000000000/flag",
        headers=headers,
    )
    assert fresh_write.status_code != 429


# -- backward compat: missing write limiter ------------------------


def test_no_write_limiter_passes_writes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the write limiter has not been installed, POST/PATCH/
    DELETE on /api/v1/* pass through unlimited. This preserves
    backward compat with existing test fixtures (and pre-T182
    deploys) that only configure read + ingest."""
    _set_required_env(monkeypatch)
    reset_rate_limiter()
    app.dependency_overrides[get_pairing_store] = lambda: PairingStore()
    try:
        client = TestClient(app)
        headers = {"Authorization": "Bearer token-A"}
        for _ in range(10):
            resp = client.post(
                "/api/v1/insights/"
                "00000000-0000-0000-0000-000000000000/flag",
                headers=headers,
            )
            assert resp.status_code != 429
    finally:
        app.dependency_overrides.clear()
