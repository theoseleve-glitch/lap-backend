import os
import uuid
from collections.abc import Awaitable, Callable

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware

from app.logging import configure_logging, logger
from app.rate_limit import rate_limit_middleware
from app.routers import admin as admin_router
from app.routers import benchmark as benchmark_router
from app.routers import billing as billing_router
from app.routers import bridge as bridge_router
from app.routers import catalog as catalog_router
from app.routers import coaching as coaching_router
from app.routers import compare_cases as compare_cases_router
from app.routers import devices as devices_router
from app.routers import driver_profile as driver_profile_router
from app.routers import endurance as endurance_router
from app.routers import insights as insights_router
from app.routers import jobs as jobs_router
from app.routers import me as me_router
from app.routers import outcomes as outcomes_router
from app.routers import pairing as pairing_router
from app.routers import prescriptions as prescriptions_router
from app.routers import sessions as sessions_router
from app.routers import skills as skills_router
from app.routers import target_events as target_events_router
from app.routers import timeline as timeline_router
from app.routers import weaknesses as weaknesses_router
from app.routers import webhooks as webhooks_router
from infra.sentry import configure_sentry

configure_logging()
configure_sentry(os.getenv("SENTRY_DSN", ""))

REQUEST_ID_HEADER = "x-request-id"

# T226 — relocate OpenAPI spec under /api/v1/ so the schema
# lives next to the routes it describes.
# T227/T228 — Swagger UI and ReDoc mounted next to the spec so
# both human-facing viewers share one versioned base URL.
app = FastAPI(
    openapi_url="/api/v1/openapi.json",
    docs_url="/api/v1/docs",
    redoc_url="/api/v1/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://app.lap-coach.racing"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(BaseHTTPMiddleware, dispatch=rate_limit_middleware)


@app.middleware("http")
async def log_requests(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    response = await call_next(request)
    logger.info(
        "http_request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
    )
    return response


@app.middleware("http")
async def attach_request_id(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    incoming = request.headers.get(REQUEST_ID_HEADER)
    request_id = incoming if incoming else str(uuid.uuid4())
    structlog.contextvars.bind_contextvars(request_id=request_id)
    try:
        response = await call_next(request)
    finally:
        structlog.contextvars.clear_contextvars()
    response.headers[REQUEST_ID_HEADER] = request_id
    return response


app.include_router(admin_router.router)
app.include_router(benchmark_router.router)
app.include_router(billing_router.router)
app.include_router(bridge_router.router)
app.include_router(bridge_router.driver_router)
app.include_router(catalog_router.router)
app.include_router(coaching_router.router)
app.include_router(compare_cases_router.router)
app.include_router(devices_router.router)
app.include_router(driver_profile_router.router)
app.include_router(endurance_router.router)
app.include_router(endurance_router.driver_router)
app.include_router(insights_router.router)
app.include_router(jobs_router.router)
app.include_router(me_router.router)
app.include_router(outcomes_router.router)
app.include_router(pairing_router.router)
app.include_router(prescriptions_router.router)
app.include_router(prescriptions_router.driver_router)
app.include_router(sessions_router.router)
app.include_router(skills_router.router)
app.include_router(target_events_router.driver_router)
app.include_router(timeline_router.router)
app.include_router(weaknesses_router.router)
app.include_router(webhooks_router.router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> dict[str, str]:
    return {"status": "ready"}


@app.get("/metrics")
async def metrics() -> Response:
    # T175 — Prometheus exposition. Mounted at root (not /api/v1)
    # so standard scrape configs work without per-app relabeling.
    # T176/T177 add custom counters and histograms; this just wires
    # the default registry (process collector + future user metrics).
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
