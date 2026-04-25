"""Admin endpoints (T161+).

`is_admin` flag on User is the gate. Tools here are operational —
diagnose drift, force-sync from Stripe, manually trigger reconciles.
Not exposed to regular users.
"""

from typing import Annotated, Any
from uuid import UUID

import stripe
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from app.logging import logger
from infra.orm import User as UserRow

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


class RefundRequest(BaseModel):
    """Operator-issued refund. `amount` (in the smallest currency unit,
    e.g. cents) is optional — omitting it asks Stripe for a full
    refund of the original charge."""

    charge_id: str = Field(..., min_length=1)
    amount: int | None = Field(default=None, ge=1)


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


@router.post("/billing/refund")
async def refund_charge(
    body: RefundRequest,
    caller: Annotated[UserRow, Depends(get_current_user)],
) -> dict[str, str | int]:
    """Issue a Stripe refund against a charge. Full refund by default;
    pass `amount` (smallest currency unit) for a partial. The
    endpoint is a thin relay — no DB writes, no user resolution."""
    _require_admin(caller)
    kwargs: dict[str, Any] = {"charge": body.charge_id}
    if body.amount is not None:
        kwargs["amount"] = body.amount
    refund: Any = stripe.Refund.create(**kwargs)
    refund_id = str(refund.get("id") or "")
    refund_status = str(refund.get("status") or "")
    refund_amount = int(refund.get("amount") or 0)
    logger.info(
        "admin_billing_refund_issued",
        admin_user_id=str(caller.id),
        charge_id=body.charge_id,
        refund_id=refund_id,
        amount=refund_amount,
        status=refund_status,
    )
    return {
        "refund_id": refund_id,
        "status": refund_status,
        "amount": refund_amount,
    }
