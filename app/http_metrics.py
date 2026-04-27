"""HTTP request counter middleware (T193).

Increments `infra.metrics.http_requests_total{method, status_class}`
on every request — both responses and unhandled exceptions.

Why exception counting matters: Starlette's `ServerErrorMiddleware`
sits OUTSIDE user-added middleware, so when a handler raises an
unhandled exception it propagates past us before being converted
into a 500. Without a try/except clause, the very thing the SLO
alert pages on (sustained 5xx) would never increment the counter.
We catch, increment with status_class="5xx", and re-raise so the
upstream behavior (Sentry, ServerErrorMiddleware) is preserved.

Why low cardinality: see infra/metrics.py:http_requests_total — only
method + status_class are labeled. Path is intentionally absent.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

from infra.metrics import http_requests_total


def _status_class(status_code: int) -> str:
    return f"{status_code // 100}xx"


async def record_http_metrics(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    try:
        response = await call_next(request)
    except Exception:
        http_requests_total.labels(
            method=request.method,
            status_class="5xx",
        ).inc()
        raise
    http_requests_total.labels(
        method=request.method,
        status_class=_status_class(response.status_code),
    ).inc()
    return response
