from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db
from infra.clerk_webhooks import (
    InvalidClerkEventError,
    apply_clerk_user_event,
    parse_event,
)

router = APIRouter(prefix="/api/v1/auth", tags=["webhooks"])


@router.post("/webhooks")
async def receive_clerk_webhook(
    payload: Annotated[dict[str, Any], Body()],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    try:
        event = parse_event(payload)
        await apply_clerk_user_event(db, event)
    except InvalidClerkEventError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return {"status": "ok"}
