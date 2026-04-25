from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from lap_schemas.v1 import SessionIngestAccepted

from app.deps import get_current_user
from infra.orm import User as UserRow

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


@router.post(
    "/ingest",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SessionIngestAccepted,
)
async def ingest_session(
    request: Request,
    user: Annotated[UserRow, Depends(get_current_user)],
) -> SessionIngestAccepted:
    # T096 ships the bare-bones acceptor: authenticated callers may POST
    # multipart bodies and the server acks. T097 adds manifest parsing,
    # T098/T099 add idempotency, T100 adds chunk persistence. Today the
    # endpoint is intentionally permissive — the caller is who they say
    # (auth dep enforced) but the body shape is not yet constrained.
    _ = user, request
    return SessionIngestAccepted(status="accepted")
