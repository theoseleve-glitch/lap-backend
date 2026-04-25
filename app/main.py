import uuid
from collections.abc import Awaitable, Callable

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.logging import configure_logging, logger
from app.rate_limit import rate_limit_middleware
from app.routers import billing as billing_router
from app.routers import catalog as catalog_router
from app.routers import devices as devices_router
from app.routers import jobs as jobs_router
from app.routers import me as me_router
from app.routers import pairing as pairing_router
from app.routers import sessions as sessions_router
from app.routers import webhooks as webhooks_router

configure_logging()

REQUEST_ID_HEADER = "x-request-id"

app = FastAPI()

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


app.include_router(billing_router.router)
app.include_router(catalog_router.router)
app.include_router(devices_router.router)
app.include_router(jobs_router.router)
app.include_router(me_router.router)
app.include_router(pairing_router.router)
app.include_router(sessions_router.router)
app.include_router(webhooks_router.router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> dict[str, str]:
    return {"status": "ready"}
