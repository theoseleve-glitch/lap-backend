import json
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Response, status
from lap_schemas.v1 import SessionIngestAccepted, SessionManifest
from pydantic import ValidationError

from app.deps import get_current_user, get_idempotency_cache
from infra.idempotency_cache import IdempotencyCache
from infra.orm import User as UserRow

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


@router.post(
    "/ingest",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SessionIngestAccepted,
)
async def ingest_session(
    manifest: Annotated[str, Form()],
    user: Annotated[UserRow, Depends(get_current_user)],
    cache: Annotated[IdempotencyCache, Depends(get_idempotency_cache)],
    idempotency_key: Annotated[
        str | None, Header(alias="idempotency-key")
    ] = None,
) -> Response | SessionIngestAccepted:
    # T097: validate manifest against lap_schemas.v1.SessionManifest.
    # T098: require Idempotency-Key header.
    # T099: replay cached response on key hit (24h TTL); the cache is
    # populated only after the manifest validates, so a malformed body
    # never poisons the slot.
    _ = user
    if idempotency_key is None or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="idempotency_key_required",
        )
    cached = cache.get(idempotency_key)
    if cached is not None:
        return Response(
            content=cached,
            status_code=status.HTTP_202_ACCEPTED,
            media_type="application/json",
        )
    try:
        data = json.loads(manifest)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="manifest_invalid_json",
        ) from exc
    try:
        SessionManifest.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="manifest_invalid",
        ) from exc
    response = SessionIngestAccepted(status="accepted")
    cache.put(idempotency_key, response.model_dump_json().encode())
    return response
