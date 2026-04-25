"""Billing endpoints — Stripe Checkout + portal (T148+).

T148 wires `POST /api/v1/billing/checkout`. Subsequent tasks add
trial metadata (T149), success/cancel URLs (T150), webhooks (T151+),
and the billing portal (T157).
"""

import json
from typing import Annotated, Any
from uuid import UUID

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from lap_schemas.v1 import (
    BillingCheckoutRequest,
    BillingCheckoutResponse,
    BillingPlan,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config
from app.deps import get_config, get_current_user, get_db
from app.logging import logger
from infra.orm import User as UserRow

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])


def _resolve_price_id(plan: BillingPlan, cfg: Config) -> str:
    mapping: dict[BillingPlan, str] = {
        "lap_monthly": cfg.stripe_price_lap_monthly,
        "lap_yearly": cfg.stripe_price_lap_yearly,
        "waitlist_founder": cfg.stripe_price_waitlist_founder,
    }
    price_id = mapping[plan]
    if not price_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="stripe_price_not_configured",
        )
    return price_id


@router.post(
    "/checkout",
    response_model=BillingCheckoutResponse,
)
async def create_checkout_session(
    body: BillingCheckoutRequest,
    user: Annotated[UserRow, Depends(get_current_user)],
    cfg: Annotated[Config, Depends(get_config)],
) -> BillingCheckoutResponse:
    price_id = _resolve_price_id(body.plan, cfg)
    base = cfg.app_base_url.rstrip("/")
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        client_reference_id=str(user.id),
        metadata={"user_id": str(user.id)},
        subscription_data={"trial_period_days": 14},
        success_url=f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base}/billing/canceled",
    )
    return BillingCheckoutResponse(
        url=str(session.url),
        session_id=str(session.id),
    )


async def _set_subscription_status_from_metadata(
    db: AsyncSession,
    metadata: dict[str, str] | None,
    new_status: str,
) -> None:
    """Look up a `User` by `metadata.user_id` and write the new
    subscription_status. No-op if the user_id is missing or
    doesn't exist locally — Stripe events for users we never knew
    about must not 5xx, otherwise Stripe retries indefinitely."""
    if not metadata:
        return
    raw_user_id = metadata.get("user_id")
    if not raw_user_id:
        return
    try:
        user_id = UUID(raw_user_id)
    except ValueError:
        logger.warning(
            "stripe_webhook_user_id_not_uuid", raw_user_id=raw_user_id
        )
        return
    user = (
        await db.execute(select(UserRow).where(UserRow.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        logger.info("stripe_webhook_user_not_found", user_id=raw_user_id)
        return
    user.subscription_status = new_status
    await db.commit()


@router.post("/webhooks", status_code=status.HTTP_200_OK)
async def receive_stripe_webhook(
    request: Request,
    cfg: Annotated[Config, Depends(get_config)],
    db: Annotated[AsyncSession, Depends(get_db)],
    stripe_signature: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    """Stripe webhook receiver. Verifies the `Stripe-Signature`
    header against the raw body and rejects on mismatch. T152+
    add per-event-type handlers; for now this only validates."""
    if stripe_signature is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing_stripe_signature",
        )
    secret = cfg.stripe_webhook_secret.get_secret_value()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="stripe_webhook_secret_not_configured",
        )
    payload = await request.body()
    try:
        # Verifies HMAC + timestamp; raises on tamper or wrong key.
        # Result discarded — handler reads `event_dict` (plain JSON)
        # below since `StripeObject` is awkward to traverse.
        stripe.Webhook.construct_event(  # type: ignore[no-untyped-call]
            payload=payload,
            sig_header=stripe_signature,
            secret=secret,
        )
    except (
        stripe.SignatureVerificationError,
        ValueError,
    ) as exc:
        logger.warning("stripe_webhook_signature_invalid", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_stripe_signature",
        ) from exc
    # Stripe's `event` is a StripeObject (not a dict) and resists
    # `.get()` / `dict(...)` conversion. Re-parse the raw JSON for
    # handler logic — verification has already happened above.
    event_dict: dict[str, Any] = json.loads(payload)
    event_type = event_dict["type"]
    logger.info(
        "stripe_webhook_received",
        event_id=event_dict["id"],
        event_type=event_type,
    )
    if event_type == "customer.subscription.created":
        sub = event_dict["data"]["object"]
        await _set_subscription_status_from_metadata(
            db, sub.get("metadata"), "trialing"
        )
    elif event_type == "customer.subscription.updated":
        sub = event_dict["data"]["object"]
        sub_status = sub.get("status")
        if sub_status:
            await _set_subscription_status_from_metadata(
                db, sub.get("metadata"), sub_status
            )
    return {"status": "received"}
