from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config
from app.deps import get_config, get_db
from infra.clerk_webhooks import (
    InvalidClerkEventError,
    InvalidSignatureError,
    apply_clerk_user_event,
    parse_event,
    verify_svix_signature,
)

router = APIRouter(prefix="/api/v1/auth", tags=["webhooks"])


async def verify_clerk_webhook_signature(
    request: Request,
    config: Annotated[Config, Depends(get_config)],
) -> None:
    body = await request.body()
    try:
        verify_svix_signature(
            secret=config.clerk_webhook_secret.get_secret_value(),
            svix_id=request.headers.get("svix-id"),
            svix_timestamp=request.headers.get("svix-timestamp"),
            svix_signature=request.headers.get("svix-signature"),
            body=body,
        )
    except InvalidSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc


@router.post("/webhooks", dependencies=[Depends(verify_clerk_webhook_signature)])
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
