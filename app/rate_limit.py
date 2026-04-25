from collections.abc import Awaitable, Callable
from typing import TypeVar

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from infra.rate_limiter import RateLimiter

F = TypeVar("F", bound=Callable[..., object])

AUTH_PATH_PREFIX = "/api/v1/auth/"

_rate_limiter: RateLimiter | None = None


def set_rate_limiter(limiter: RateLimiter) -> None:
    """Install a process-wide rate limiter. Called by production startup
    or by tests that want to exercise the rate-limited path."""
    global _rate_limiter
    _rate_limiter = limiter


def reset_rate_limiter() -> None:
    """Clear the installed rate limiter so the middleware bypasses
    enforcement. Used by tests to isolate state between cases."""
    global _rate_limiter
    _rate_limiter = None


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


async def rate_limit_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    if _rate_limiter is None:
        return await call_next(request)
    if not request.url.path.startswith(AUTH_PATH_PREFIX):
        return await call_next(request)
    ip = _client_ip(request)
    if not _rate_limiter.check(f"auth:{ip}"):
        return JSONResponse(
            status_code=429,
            content={"detail": "rate_limited"},
        )
    return await call_next(request)
