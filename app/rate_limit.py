from collections.abc import Awaitable, Callable
from typing import TypeVar

from fastapi import Request, Response

F = TypeVar("F", bound=Callable[..., object])


def rate_limit(limit: str) -> Callable[[F], F]:
    def decorator(func: F) -> F:
        func.__rate_limit__ = limit  # type: ignore[attr-defined]
        return func

    return decorator


async def rate_limit_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    return await call_next(request)
