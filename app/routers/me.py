from typing import Annotated, cast

from fastapi import APIRouter, Depends
from lap_schemas.v1 import User as UserSchema
from lap_schemas.v1.user import SubscriptionStatus

from app.deps import get_current_user
from infra.orm import User as UserRow

router = APIRouter(prefix="/api/v1", tags=["me"])


@router.get("/me", response_model=UserSchema)
async def read_me(
    user: Annotated[UserRow, Depends(get_current_user)],
) -> UserSchema:
    return UserSchema(
        id=user.id,
        clerk_user_id=user.clerk_user_id,
        email=user.email,
        created_at=user.created_at,
        subscription_status=cast(SubscriptionStatus, user.subscription_status),
        is_admin=user.is_admin,
    )
