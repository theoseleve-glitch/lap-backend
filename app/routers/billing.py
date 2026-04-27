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
    BillingPortalResponse,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config
from app.deps import get_config, get_current_user, get_db
from app.error_codes import ErrorCode
from app.logging import logger
from infra.notifications import (
    Notification,
    NotificationQueue,
    get_notification_queue,
)
from infra.orm import User as UserRow

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])


def _resolve_waitlist_promo_id(cfg: Config) -> str | None:
    """Translate the human-readable promotion code stored in
    `cfg.stripe_promo_code_waitlist_founder` into the Stripe
    promotion-code object ID required by `discounts[].promotion_code`.
    Returns None if the code is unset or no live match exists —
    checkout proceeds without a discount rather than 5xx-ing on a
    misconfigured cohort gift."""
    code = cfg.stripe_promo_code_waitlist_founder.strip()
    if not code:
        return None
    matches = stripe.PromotionCode.list(
        code=code, active=True, limit=1
    )
    if not matches.data:
        logger.info("waitlist_promo_code_not_found", code=code)
        return None
    return str(matches.data[0].id)


_B2B_PLANS: frozenset[BillingPlan] = frozenset(
    {"b2b_5seat", "b2b_15seat", "b2b_unlimited"}
)
# T148 — consumer trial; T178 — B2B trial (longer because team
# onboarding takes more than individual eval).
_CONSUMER_TRIAL_DAYS = 14
_B2B_TRIAL_DAYS = 30


def _trial_days_for(plan: BillingPlan) -> int:
    return _B2B_TRIAL_DAYS if plan in _B2B_PLANS else _CONSUMER_TRIAL_DAYS


def _resolve_price_id(plan: BillingPlan, cfg: Config) -> str:
    mapping: dict[BillingPlan, str] = {
        "lap_monthly": cfg.stripe_price_lap_monthly,
        "lap_yearly": cfg.stripe_price_lap_yearly,
        "waitlist_founder": cfg.stripe_price_waitlist_founder,
        # T177 — B2B tiers
        "b2b_5seat": cfg.stripe_price_b2b_5seat,
        "b2b_15seat": cfg.stripe_price_b2b_15seat,
        "b2b_unlimited": cfg.stripe_price_b2b_unlimited,
    }
    price_id = mapping[plan]
    if not price_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ErrorCode.STRIPE_PRICE_NOT_CONFIGURED,
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

    create_kwargs: dict[str, Any] = {
        "mode": "subscription",
        "line_items": [{"price": price_id, "quantity": 1}],
        "client_reference_id": str(user.id),
        "metadata": {"user_id": str(user.id)},
        "subscription_data": {
            "trial_period_days": _trial_days_for(body.plan)
        },
        "success_url": (
            f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}"
        ),
        "cancel_url": f"{base}/billing/canceled",
    }
    if user.is_waitlist:
        promo_id = _resolve_waitlist_promo_id(cfg)
        if promo_id is not None:
            create_kwargs["discounts"] = [{"promotion_code": promo_id}]
    # T179 — B2B orgs need monthly invoice PDFs emailed to a known
    # billing address, with a full billing address collected at
    # checkout so the PDFs satisfy VAT / sales-tax accounting.
    # Stripe auto-emails the invoice once each charge succeeds —
    # no extra wiring on our side.
    if body.plan in _B2B_PLANS:
        create_kwargs["customer_email"] = user.email
        create_kwargs["billing_address_collection"] = "required"

    session = stripe.checkout.Session.create(**create_kwargs)
    return BillingCheckoutResponse(
        url=str(session.url),
        session_id=str(session.id),
    )


@router.get("/portal", response_model=BillingPortalResponse)
async def create_billing_portal_session(
    user: Annotated[UserRow, Depends(get_current_user)],
    cfg: Annotated[Config, Depends(get_config)],
) -> BillingPortalResponse:
    if not user.stripe_customer_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ErrorCode.NO_STRIPE_CUSTOMER,
        )
    base = cfg.app_base_url.rstrip("/")
    portal = stripe.billing_portal.Session.create(
        customer=user.stripe_customer_id,
        return_url=f"{base}/billing",
    )
    return BillingPortalResponse(url=str(portal.url))


async def _set_subscription_status_for_user_id(
    db: AsyncSession,
    raw_user_id: str,
    new_status: str,
) -> bool:
    """Look up a `User` by id-string and write the new
    subscription_status. Returns True if the user was found and
    the write happened, False otherwise. No-op (False) for missing
    or malformed user_ids — Stripe events for ghost users must not
    5xx, otherwise Stripe retries indefinitely."""
    try:
        user_id = UUID(raw_user_id)
    except ValueError:
        logger.warning(
            "stripe_webhook_user_id_not_uuid", raw_user_id=raw_user_id
        )
        return False
    user = (
        await db.execute(select(UserRow).where(UserRow.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        logger.info("stripe_webhook_user_not_found", user_id=raw_user_id)
        return False
    user.subscription_status = new_status
    await db.commit()
    return True


async def _set_subscription_status_from_metadata(
    db: AsyncSession,
    metadata: dict[str, str] | None,
    new_status: str,
) -> None:
    """Convenience wrapper: extract `user_id` from a Stripe
    metadata dict and apply the status update."""
    if not metadata:
        return
    raw_user_id = metadata.get("user_id")
    if not raw_user_id:
        return
    await _set_subscription_status_for_user_id(db, raw_user_id, new_status)


async def _set_user_stripe_customer_id(
    db: AsyncSession,
    raw_user_id: str,
    stripe_customer_id: str,
) -> None:
    """Record the user's Stripe customer ID so the billing portal
    endpoint can resolve it later. No-op if the user_id is malformed
    or doesn't resolve."""
    try:
        user_id = UUID(raw_user_id)
    except ValueError:
        return
    user = (
        await db.execute(select(UserRow).where(UserRow.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        return
    user.stripe_customer_id = stripe_customer_id
    await db.commit()


def _user_id_from_invoice(invoice: dict[str, Any]) -> str | None:
    """Pull `user_id` out of the various Stripe invoice metadata
    locations — `subscription_details.metadata` (current API) or
    plain `metadata` (older / explicitly-set)."""
    sub_details = invoice.get("subscription_details") or {}
    sub_md = sub_details.get("metadata") or {}
    if "user_id" in sub_md:
        return str(sub_md["user_id"])
    md = invoice.get("metadata") or {}
    if "user_id" in md:
        return str(md["user_id"])
    return None


@router.post("/webhooks", status_code=status.HTTP_200_OK)
async def receive_stripe_webhook(
    request: Request,
    cfg: Annotated[Config, Depends(get_config)],
    db: Annotated[AsyncSession, Depends(get_db)],
    notif_q: Annotated[NotificationQueue, Depends(get_notification_queue)],
    stripe_signature: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    """Stripe webhook receiver. Verifies the `Stripe-Signature`
    header against the raw body and rejects on mismatch. T152+
    add per-event-type handlers; for now this only validates."""
    if stripe_signature is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorCode.MISSING_STRIPE_SIGNATURE,
        )
    secret = cfg.stripe_webhook_secret.get_secret_value()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ErrorCode.STRIPE_WEBHOOK_SECRET_NOT_CONFIGURED,
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
            detail=ErrorCode.INVALID_STRIPE_SIGNATURE,
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
        metadata = sub.get("metadata") or {}
        raw_user_id = metadata.get("user_id")
        customer_id = sub.get("customer")
        if raw_user_id:
            await _set_subscription_status_for_user_id(
                db, raw_user_id, "trialing"
            )
            if customer_id:
                await _set_user_stripe_customer_id(
                    db, raw_user_id, str(customer_id)
                )
    elif event_type == "customer.subscription.updated":
        sub = event_dict["data"]["object"]
        sub_status = sub.get("status")
        if sub_status:
            await _set_subscription_status_from_metadata(
                db, sub.get("metadata"), sub_status
            )
    elif event_type == "customer.subscription.deleted":
        sub = event_dict["data"]["object"]
        await _set_subscription_status_from_metadata(
            db, sub.get("metadata"), "canceled"
        )
    elif event_type == "invoice.payment_failed":
        invoice = event_dict["data"]["object"]
        user_id = _user_id_from_invoice(invoice)
        if user_id:
            updated = await _set_subscription_status_for_user_id(
                db, user_id, "past_due"
            )
            if updated:
                notif_q.enqueue(
                    Notification(
                        kind="payment_failed",
                        user_id=user_id,
                        payload={
                            "invoice_id": str(invoice.get("id", "")),
                            "amount_due": invoice.get("amount_due", 0),
                            "currency": str(invoice.get("currency", "")),
                        },
                    )
                )
    elif event_type == "invoice.payment_succeeded":
        invoice = event_dict["data"]["object"]
        user_id = _user_id_from_invoice(invoice)
        if user_id:
            await _set_subscription_status_for_user_id(
                db, user_id, "active"
            )
    return {"status": "received"}
