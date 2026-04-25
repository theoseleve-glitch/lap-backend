import json
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, status
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
) -> SessionIngestAccepted:
    # T097: parse the `manifest` form field as JSON, validate against
    # lap_schemas.v1.SessionManifest. T098/T099 will add idempotency,
    # T100 will persist chunks. The user is still required (auth dep)
    # but body shape is now contract-bound to the public schema.
    _ = user
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
