"""T237 — env-backed feature flags for gradual rollouts.

Convention:
    FLAG_<UPPER_NAME> in the environment, value in {1, true, yes, on}
    (case-insensitive) means the flag is enabled. Anything else,
    or unset, means disabled.

Strict truthy allowlist — `bool("disabled")` is True in Python,
so a free-form match would silently turn a flag on when an
operator typed `FLAG_X=disabled` thinking they were turning it
off. The allowlist makes the wrong typing fail loudly (flag
stays off when they meant on) instead of dangerously (flag turns
on when they meant off).

Used by `app/routers/me.py` to gate a verbose log line on
GET /api/v1/me — see `verbose_me_read` flag.
"""

from __future__ import annotations

import os

_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def is_enabled(name: str) -> bool:
    """Return True iff `FLAG_<NAME.upper()>` env var is a truthy value."""
    raw = os.environ.get(f"FLAG_{name.upper()}", "")
    return raw.lower() in _TRUTHY
