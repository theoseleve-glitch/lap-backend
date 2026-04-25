import json
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Header, HTTPException, status
from lap_schemas.v1 import SessionIngestAccepted, SessionManifest
from pydantic import ValidationError

from app.deps import get_current_user
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
    idempotency_key: Annotated[
        str | None, Header(alias="idempotency-key")
    ] = None,
) -> SessionIngestAccepted:
    # T097: validate manifest against lap_schemas.v1.SessionManifest.
    # T098: require Idempotency-Key header (any non-empty/non-blank
    # string for now; T099 will use it as the cache key for replay
    # detection, T100 will persist chunks).
    _ = user
    if idempotency_key is None or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="idempotency_key_required",
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
    return SessionIngestAccepted(status="accepted")
