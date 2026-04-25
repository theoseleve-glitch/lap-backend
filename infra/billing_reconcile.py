"""Daily Stripe ↔ DB reconciliation (T160).

Pages through every Stripe subscription, looks up the matching
local user via `metadata.user_id`, and logs any cases where
`subscription_status` has drifted out of sync with Stripe. Used as
a defense-in-depth net for missed/replayed webhooks.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import stripe
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.logging import logger
from infra.orm import User as UserRow


@dataclass(frozen=True)
class ReconcileResult:
    checked_count: int
    drift_count: int
    skipped_count: int


async def reconcile_billing(
    db_factory: async_sessionmaker[Any],
) -> ReconcileResult:
    """Compare every active Stripe subscription to its local user.
    Returns a `ReconcileResult` summarizing what we saw; emits one
    `billing_reconcile_drift` log per mismatch."""
    listing = stripe.Subscription.list(limit=100)
    checked = 0
    drift = 0
    skipped = 0
    async with db_factory() as session:
        for sub_any in listing.auto_paging_iter():
            sub: Any = sub_any  # tests pass dicts, prod yields StripeObjects
            metadata = sub.get("metadata") or {}
            raw_user_id = metadata.get("user_id")
            if not raw_user_id:
                skipped += 1
                continue
            try:
                user_id = UUID(str(raw_user_id))
            except ValueError:
                skipped += 1
                continue
            user = (
                await session.execute(
                    select(UserRow).where(UserRow.id == user_id)
                )
            ).scalar_one_or_none()
            if user is None:
                skipped += 1
                continue
            checked += 1
            stripe_status = str(sub.get("status") or "")
            if stripe_status and user.subscription_status != stripe_status:
                drift += 1
                logger.warning(
                    "billing_reconcile_drift",
                    user_id=str(user_id),
                    db_status=user.subscription_status,
                    stripe_status=stripe_status,
                    subscription_id=str(sub.get("id") or ""),
                )
    logger.info(
        "billing_reconcile_complete",
        checked=checked,
        drift=drift,
        skipped=skipped,
    )
    return ReconcileResult(
        checked_count=checked, drift_count=drift, skipped_count=skipped
    )
