"""Tiered rate-limit middleware.

Four independent buckets:

- auth: per-IP. Applied to /api/v1/auth/*. Pre-T014.
- read: per-user (Authorization-header keyed; IP fallback). Applied
  to GET on /api/v1/* (except auth). T014, narrowed by T182.
- write: per-user. Applied to POST/PATCH/PUT/DELETE on /api/v1/*
  (except auth and the ingest path). Different load profile from
  read so it gets its own bucket — a chatty UI poll loop on GETs
  must not starve the same user's ability to save state. T182.
- ingest: per-user. Applied to POST /api/v1/sessions/ingest only.
  Window is hours, not minutes — uploads are heavy. T014.

A tier is enforced only when its limiter has been installed via
the corresponding setter. With no setters called, the middleware
is a pass-through — existing test fixtures that never configure a
limiter keep working unchanged.

User identity is the verbatim Authorization header value (cheap;
the full JWT verify lives behind `get_current_user` which the
handler invokes anyway). Anonymous flooders are bucketed by IP so
they're still throttled.
"""

from collections.abc import Awaitable, Callable
from typing import TypeVar

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from app.error_codes import ErrorCode
from infra.rate_limiter import RateLimiter

F = TypeVar("F", bound=Callable[..., object])

AUTH_PATH_PREFIX = "/api/v1/auth/"
INGEST_PATH = "/api/v1/sessions/ingest"
API_V1_PREFIX = "/api/v1/"
# T199 — docs / spec endpoints are public reference material. They
# share the /api/v1/ prefix with the rate-limited routes but must
# be exempt: the Swagger UI fetches `openapi.json` on every page
# reload, and a developer iterating quickly would burn the read
# bucket and start seeing 429s on the docs themselves. Exempting
# them by exact path keeps the matcher cheap (one `in` check per
# request, no startswith fan-out).
DOCS_EXEMPT_PATHS = frozenset(
    {"/api/v1/docs", "/api/v1/redoc", "/api/v1/openapi.json"}
)

_auth_limiter: RateLimiter | None = None
_read_limiter: RateLimiter | None = None
_write_limiter: RateLimiter | None = None
_ingest_limiter: RateLimiter | None = None


def set_rate_limiter(limiter: RateLimiter) -> None:
    """Install the auth-tier limiter. Existing API kept for
    backward compat with pre-T014 callers."""
    global _auth_limiter
    _auth_limiter = limiter


def set_read_rate_limiter(limiter: RateLimiter) -> None:
    """Install the read-tier limiter (T014)."""
    global _read_limiter
    _read_limiter = limiter


def set_write_rate_limiter(limiter: RateLimiter) -> None:
    """Install the write-tier limiter (T182)."""
    global _write_limiter
    _write_limiter = limiter


def set_ingest_rate_limiter(limiter: RateLimiter) -> None:
    """Install the ingest-tier limiter (T014)."""
    global _ingest_limiter
    _ingest_limiter = limiter


def reset_rate_limiter() -> None:
    """Clear ALL installed limiters so the middleware bypasses
    enforcement. Used by tests to isolate state between cases."""
    global _auth_limiter, _read_limiter, _write_limiter, _ingest_limiter
    _auth_limiter = None
    _read_limiter = None
    _write_limiter = None
    _ingest_limiter = None


def rate_limit(limit: str) -> Callable[[F], F]:
    def decorator(func: F) -> F:
        func.__rate_limit__ = limit  # type: ignore[attr-defined]
        return func

    return decorator


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # Standard X-F-F format is a comma-separated list with the
        # original client first. Trust the first hop only — anything
        # downstream is under our reverse-proxy's control.
        return forwarded.split(",")[0].strip()
    if request.client is not None:
        return request.client.host
    return "unknown"


def _user_key(request: Request) -> str:
    """Per-user key for the read/ingest tiers. Uses the verbatim
    Authorization header so two distinct tokens get distinct
    buckets without any JWT decoding cost. Falls back to IP for
    unauthenticated requests so anonymous flooders are still
    throttled (they shouldn't be hitting authed endpoints anyway)."""
    auth = request.headers.get("authorization")
    if auth:
        return f"auth:{auth}"
    return f"ip:{_client_ip(request)}"


_WRITE_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})


def _resolve_tier(
    request: Request,
) -> tuple[RateLimiter | None, str]:
    """Pick the (limiter, key) for this request. Returns (None, "")
    when no tier applies — the middleware short-circuits.

    Tier selection (most-specific to least):
      docs paths  → no limiter (T199 — public reference material)
      auth path   → auth tier (per-IP)
      ingest path → ingest tier (per-user)
      /api/v1/*   → write tier if method mutates, else read tier
      anything else (health, metrics) → no limiter
    """
    path = request.url.path
    if path in DOCS_EXEMPT_PATHS:
        return None, ""
    if path.startswith(AUTH_PATH_PREFIX):
        if _auth_limiter is None:
            return None, ""
        return _auth_limiter, f"auth:{_client_ip(request)}"
    if path == INGEST_PATH:
        if _ingest_limiter is None:
            return None, ""
        return _ingest_limiter, f"ingest:{_user_key(request)}"
    if path.startswith(API_V1_PREFIX):
        if request.method in _WRITE_METHODS:
            if _write_limiter is None:
                return None, ""
            return _write_limiter, f"write:{_user_key(request)}"
        if _read_limiter is None:
            return None, ""
        return _read_limiter, f"read:{_user_key(request)}"
    # health, metrics, etc. — never rate-limited.
    return None, ""


async def rate_limit_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    limiter, key = _resolve_tier(request)
    if limiter is None:
        return await call_next(request)
    if not limiter.check(key):
        return JSONResponse(
            status_code=429,
            content={"detail": ErrorCode.RATE_LIMITED},
        )
    return await call_next(request)
