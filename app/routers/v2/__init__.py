"""T238 / T188 — v2 router scaffold.

The first v2 endpoint will land here as `app/routers/v2/<name>.py`
mirroring the v1 convention (`app/routers/<name>.py`), so the
import statement reads naturally as
`from app.routers.v2 import <name> as <name>_router`.

This module exposes a single empty `APIRouter` with prefix
`/api/v2` (T188). Future v2 resource modules either:
  - register their endpoints on this `router` directly via
    `from app.routers.v2 import router`, or
  - define their own sub-router and have the resource module
    re-export it for `app/main.py` to include alongside.

Why a scaffold and not just "we'll create it when we need it":
- Clients pin to `/api/v1` today. The contract is "v1 lives here,
  v2 lives there." Without the scaffold there's no "there" — when
  the time comes to add a v2 endpoint, someone has to invent the
  layout under deadline pressure and might mix v1 and v2 in the
  same module.
- Tests in `tests/test_api_versioning.py` enforce this layout:
  every router under `app/routers/` (excluding subpackages like
  this one) must use `/api/v1/`. v2 lives here, v1 lives there.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/v2", tags=["v2"])
