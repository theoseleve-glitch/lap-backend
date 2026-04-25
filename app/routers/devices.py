from typing import Annotated

from fastapi import APIRouter, Depends
from lap_schemas.v1 import DeviceSession as DeviceSessionSchema
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from infra.orm import DeviceSession as DeviceSessionRow
from infra.orm import User as UserRow

router = APIRouter(prefix="/api/v1/auth/devices", tags=["devices"])


@router.get("", response_model=list[DeviceSessionSchema])
async def list_devices(
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[DeviceSessionSchema]:
    rows = (
        await db.execute(
            select(DeviceSessionRow).where(
                DeviceSessionRow.user_id == user.id,
                DeviceSessionRow.revoked_at.is_(None),
            )
        )
    ).scalars().all()
    return [
        DeviceSessionSchema(id=r.id, created_at=r.created_at) for r in rows
    ]
