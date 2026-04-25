"""Stripe SDK setup (T146).

Reads `stripe_secret` off the app `Config` and assigns it to
`stripe.api_key` so subsequent `stripe.*` calls authenticate as
the LAP backend. Call once at process startup.
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
