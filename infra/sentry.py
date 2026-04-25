"""Sentry SDK setup (T166).

Empty DSN → no-op so local dev and CI don't beacon out. Non-empty
DSN → standard SDK init so unhandled exceptions, 500-level FastAPI
responses, and other captured events flow to the project. Call
once at process startup.
"""

import tomllib
from pathlib import Path

import sentry_sdk

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _project_version() -> str:
    """Read the project version from pyproject.toml. The file is
    always present alongside the package — `uv sync` needs it, the
    Dockerfile copies it, and CI checks it out — so the same call
    works in every environment we deploy to."""
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def configure_sentry(dsn: str) -> None:
    """Initialize Sentry if a DSN is provided. No-op otherwise.

    Takes a raw string so it can be invoked at module-import time
    (where constructing the full `app.config.Config` would require
    every other secret to be present in env)."""
    if not dsn:
        return
    # T167 — sample 10% of transactions. Cheap by default; raise
    # via the Sentry dashboard for incident-driven deep-dive.
    # T168 — tag every event with `lap-backend@<version>` so the
    # Sentry "first seen in release" UI lights up correctly.
    sentry_sdk.init(
        dsn=dsn,
        traces_sample_rate=0.1,
        release=f"lap-backend@{_project_version()}",
    )
