"""T238 — API versioning: v1 prefix locked, v2 scaffold present.

The v1 prefix lock is a structural invariant: every router under
`app/routers/` (excluding any future v2 subpackage) must carry
the `/api/v1/` prefix. Without this enforcement a new endpoint
shipped at `/admin` (no prefix) would silently break the
versioning contract for clients that pin to `/api/v1`.

The v2 scaffold is a placeholder package at `app/routers/v2/`
so the next major version has a clear home that mirrors the
existing import convention (`from app.routers import X`).
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from fastapi import APIRouter

from app.main import app as fastapi_app  # FastAPI app instance

REPO_ROOT = Path(__file__).resolve().parents[1]

# System endpoints exempt from /api/v1/ prefix — they're operational,
# not part of the customer-facing API surface and shouldn't be
# versioned (kubelet liveness probes, prometheus scrape).
_SYSTEM_PATHS = frozenset({
    "/healthz",
    "/readyz",
    "/metrics",
    "/api/v1/openapi.json",
    "/api/v1/docs",
    "/api/v1/redoc",
    "/api/v1/docs/oauth2-redirect",  # auto-mounted by FastAPI when docs_url is set
    "/docs/oauth2-redirect",  # FastAPI mounts this at root regardless of docs_url
})


def _discovered_router_modules() -> list[str]:
    """Every module directly under app.routers (no subpackages).
    Subpackages like app.routers.v2 are excluded so future v2
    routers don't trip the v1-only assertion."""
    modules: list[str] = []
    routers_pkg = importlib.import_module("app.routers")
    for info in pkgutil.iter_modules(routers_pkg.__path__):
        if info.ispkg:
            continue
        modules.append(f"app.routers.{info.name}")
    return modules


# -- v1 prefix lock -----------------------------------------------


def test_every_v1_router_uses_api_v1_prefix() -> None:
    """Walk every module in app.routers and assert each module-level
    APIRouter has a prefix starting with `/api/v1`. New routers must
    be deliberate about which version they belong to."""
    failures: list[str] = []
    for module_name in _discovered_router_modules():
        module = importlib.import_module(module_name)
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if isinstance(attr, APIRouter) and not attr.prefix.startswith(
                "/api/v1"
            ):
                failures.append(
                    f"{module_name}.{attr_name} prefix={attr.prefix!r}"
                )
    assert not failures, (
        f"router(s) missing /api/v1 prefix: {failures}"
    )


def test_app_only_serves_v1_or_system_routes() -> None:
    """Belt-and-braces check on the assembled app: every registered
    HTTP route is either under /api/v1/ or in the system allowlist.
    This catches the case where a router with a non-v1 prefix is
    defined and *also* included via app.include_router."""
    customer_paths: list[str] = []
    for route in fastapi_app.routes:
        path = getattr(route, "path", None)
        if path is None:
            continue
        if path in _SYSTEM_PATHS:
            continue
        if not path.startswith("/api/v1/") and path != "/api/v1":
            customer_paths.append(path)
    assert customer_paths == [], (
        f"non-v1 customer-visible routes leaked: {customer_paths}"
    )


# -- v2 scaffold present ------------------------------------------


def test_v2_scaffold_package_exists() -> None:
    """The v2 scaffold lives at app/routers/v2/ — empty for now,
    but importable so the next major version's routers have a
    home that mirrors the existing convention."""
    pkg_init = REPO_ROOT / "app" / "routers" / "v2" / "__init__.py"
    assert pkg_init.is_file(), f"missing v2 scaffold at {pkg_init}"


def test_v2_scaffold_is_importable_python_package() -> None:
    """If the package exists on disk but isn't a proper Python
    package (missing __init__.py, syntax error), future imports
    would fail in surprising ways."""
    module = importlib.import_module("app.routers.v2")
    assert module is not None


def test_v2_scaffold_has_no_routers_yet() -> None:
    """Scaffold = structural placeholder, not implementation.
    Once routers land here, this test should be deleted (not
    weakened) by whoever ships the first v2 endpoint."""
    import app.routers.v2 as v2

    routers_in_v2 = [
        getattr(v2, name)
        for name in dir(v2)
        if isinstance(getattr(v2, name, None), APIRouter)
    ]
    assert routers_in_v2 == []
