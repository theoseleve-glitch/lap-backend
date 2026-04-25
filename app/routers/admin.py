"""Admin endpoints (T161+).

`is_admin` flag on User is the gate. Tools here are operational —
diagnose drift, force-sync from Stripe, manually trigger reconciles.
Not exposed to regular users.
"""

from typing import Annotated, Any
from uuid import UUID

import stripe
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from app.logging import logger
from infra.orm import User as UserRow

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


def _require_admin(user: UserRow) -> None:
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin_only",
        )


@router.post("/billing/sync/{user_id}")
async def force_sync_billing_status(
    user_id: UUID,
    caller: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    """Pull the most-recent Stripe subscription for the target
    user's customer and write its `status` to the local DB. If the
    customer has no subs, leave status unchanged (admin sync isn't
    a "reset to default" tool)."""
    _require_admin(caller)
    target = (
        await db.execute(select(UserRow).where(UserRow.id == user_id))
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="user_not_found",
        )
    if not target.stripe_customer_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no_stripe_customer",
        )
    listing = stripe.Subscription.list(
        customer=target.stripe_customer_id, limit=1
    )
    found_status: str | None = None
    for sub_any in listing.auto_paging_iter():
        sub: Any = sub_any
        found_status = str(sub.get("status") or "") or None
        break
    if found_status:
        target.subscription_status = found_status
        await db.commit()
        logger.info(
            "admin_billing_sync_applied",
            user_id=str(target.id),
            new_status=found_status,
        )
    else:
        logger.info(
            "admin_billing_sync_no_subs",
            user_id=str(target.id),
        )
    return {"subscription_status": target.subscription_status}
