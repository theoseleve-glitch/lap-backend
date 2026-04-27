"""T199 (sprint2-backend) — OpenAPI docs are accessible.

Three companion endpoints serve the API spec:
  - `/api/v1/openapi.json` — the spec itself
  - `/api/v1/docs` — Swagger UI (interactive viewer)
  - `/api/v1/redoc` — ReDoc (read-only viewer)

T227/T228 (sprint 1) pinned that they render. T199 pins the
NEXT layer: that they're *accessible* — no Authorization header
required, AND not subject to the read-tier rate limiter.

Why the rate-limit exemption is load-bearing: the read tier's
prefix matcher in `app.rate_limit._resolve_tier` is
`/api/v1/*`. Docs paths are sub-paths of that prefix, so without
an explicit exemption a developer hammering the Swagger UI
(which fetches the spec on every page reload) would start
seeing 429s once the read-bucket fills. That breaks the
"docs are reference material, available always" expectation
and turns a UI bug into a fake outage.

Pinned behaviors:
  - GET each docs path WITHOUT Authorization → 200
  - GET each docs path REPEATEDLY past the read-tier limit →
    still 200 (proves the rate-limit middleware exempts them)
"""

from __future__ import annotations

from collections.abc import Generator

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.rate_limit import reset_rate_limiter, set_read_rate_limiter
from infra.rate_limiter import RateLimiter

_DOCS_PATHS = (
    "/api/v1/openapi.json",
    "/api/v1/docs",
    "/api/v1/redoc",
)


# -- accessibility without auth ----------------------------------


@pytest.mark.parametrize("path", _DOCS_PATHS)
def test_docs_path_accessible_without_authorization_header(
    path: str,
) -> None:
    """No Authorization header at all — docs paths must serve
    200, never 401. This is the contract for "docs are public":
    a curl call without credentials must work."""
    client = TestClient(app)
    resp = client.get(path)
    assert resp.status_code == 200, (
        f"{path} returned {resp.status_code} unauthenticated; "
        "docs must be public reference material"
    )


# -- rate-limit exemption ---------------------------------------


@pytest.fixture
def tight_read_limiter() -> Generator[RateLimiter, None, None]:
    """Configure the read-tier limiter at limit=2 so the third
    hit would be blocked under normal `/api/v1/*` rules. If
    docs are correctly exempted, even the 5th hit returns 200."""
    redis_client = fakeredis.FakeRedis()
    limiter = RateLimiter(redis_client, limit=2, window_seconds=60)
    set_read_rate_limiter(limiter)
    try:
        yield limiter
    finally:
        reset_rate_limiter()


@pytest.mark.parametrize("path", _DOCS_PATHS)
def test_docs_path_not_subject_to_read_rate_limit(
    path: str,
    tight_read_limiter: RateLimiter,  # noqa: ARG001
) -> None:
    """With limit=2/min, a non-exempt /api/v1/* path returns
    429 on the 3rd identical hit. Docs paths must NOT — they're
    exempt from rate limiting entirely so the Swagger UI's
    spec-refetch-on-reload doesn't burn the developer's
    bucket."""
    client = TestClient(app)
    # Five hits without Authorization; same per-IP key for the
    # rate limiter's fallback bucket.
    for i in range(5):
        resp = client.get(path)
        assert resp.status_code == 200, (
            f"{path} returned {resp.status_code} on hit #{i + 1}; "
            "docs must NOT be rate-limited (spec refetch from "
            "the Swagger UI would break when limits are hit)"
        )


def test_non_docs_api_v1_path_is_still_rate_limited(
    tight_read_limiter: RateLimiter,  # noqa: ARG001
) -> None:
    """Counter-test: prove the rate-limit middleware is actually
    armed. If non-docs /api/v1/* paths ALSO sail through, the
    'docs exempt' test above would pass for the wrong reason
    (no rate limit running at all). With limit=2, the 3rd hit
    on a non-docs path must 429."""
    client = TestClient(app)
    statuses = []
    for _ in range(3):
        resp = client.get("/api/v1/coaching/rules")
        statuses.append(resp.status_code)
    assert 429 in statuses, (
        "expected at least one 429 from the non-docs path "
        f"with limit=2/min; got {statuses}. The docs-exempt "
        "test cannot be trusted unless the limiter is actually "
        "blocking other paths."
    )
