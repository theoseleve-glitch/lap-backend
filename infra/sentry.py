"""Sentry SDK setup (T166).

Empty DSN → no-op so local dev and CI don't beacon out. Non-empty
DSN → standard SDK init so unhandled exceptions, 500-level FastAPI
responses, and other captured events flow to the project. Call
once at process startup.
"""

import sentry_sdk


def configure_sentry(dsn: str) -> None:
    """Initialize Sentry if a DSN is provided. No-op otherwise.

    Takes a raw string so it can be invoked at module-import time
    (where constructing the full `app.config.Config` would require
    every other secret to be present in env)."""
    if not dsn:
        return
    # T167 — sample 10% of transactions. Cheap by default; raise
    # via the Sentry dashboard for incident-driven deep-dive.
    sentry_sdk.init(dsn=dsn, traces_sample_rate=0.1)
