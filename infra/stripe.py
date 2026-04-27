"""Stripe SDK setup (T146) and seat-quantity helper (T168).

Reads `stripe_secret` off the app `Config` and assigns it to
`stripe.api_key` so subsequent `stripe.*` calls authenticate as
the LAP backend. Call once at process startup.

T168 adds `update_subscription_quantity` for multi-seat org
subscriptions: the org's seat count must follow Stripe's
quantity field, with proration on add/remove.
"""

import stripe

from app.config import Config


def configure_stripe(cfg: Config) -> None:
    """Set `stripe.api_key` from `Config.stripe_secret`. Raises
    `ValueError` if the secret is empty (defensive: an unset key
    silently swallows API calls until a 401 lands in production)."""
    secret = cfg.stripe_secret.get_secret_value()
    if not secret:
        raise ValueError("stripe_secret is empty; check STRIPE_SECRET env")
    stripe.api_key = secret


def update_subscription_quantity(
    subscription_id: str, quantity: int
) -> None:
    """Sync a Stripe subscription's seat count.

    Retrieves the subscription, takes its (single) line-item id,
    and calls modify with proration. Mid-cycle adds charge a
    partial amount, removes credit a partial refund.

    `proration_behavior="create_prorations"` is explicit because
    Stripe's default depends on account settings — relying on the
    default would make billing behavior environment-dependent.
    """
    sub = stripe.Subscription.retrieve(subscription_id)
    item_id = sub["items"]["data"][0]["id"]
    stripe.Subscription.modify(
        subscription_id,
        items=[{"id": item_id, "quantity": quantity}],
        proration_behavior="create_prorations",
    )
