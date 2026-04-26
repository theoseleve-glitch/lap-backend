"""T015 (sprint2-backend) — broader OpenAPI contract pin.

Builds on T229 (which only fuzzed /healthz) by:

1. Expanding schemathesis fuzz coverage to every no-auth endpoint
   the spec declares — currently catalog (tracks/cars) plus
   coaching rules. Fuzzer can't manufacture JWT bearers, so
   auth-protected routes are out of schemathesis scope.

2. A meta-test that the OpenAPI spec is in sync with the
   FastAPI route inventory: every concrete API route (excluding
   internal docs/openapi/metrics paths) must appear in the
   spec, and every spec path must map back to a real route.
   Catches "drift" of the third kind — spec missing routes
   handlers actually answer, or vice versa.

"Zero drift" is the sprint contract: a contract test fails the
build the moment a route is added/removed/renamed without the
spec keeping pace.
"""

from __future__ import annotations

import os
import re
from typing import Any

import schemathesis
from fastapi.routing import APIRoute

# Schemathesis loads the OpenAPI spec at import time, which forces
# `app.main` to import → eager Config/JWKS dependency wiring. Set
# minimum-viable env BEFORE the import so the spec generation
# doesn't crash at collection time.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://x")
os.environ.setdefault("CLERK_SECRET", "sk_test_clerk")
os.environ.setdefault("STRIPE_SECRET", "sk_test_stripe")

from app.main import app  # noqa: E402

# -- Part 1: schemathesis fuzz on no-auth endpoints ----------------

# Routes the fuzzer can hit without bearer tokens — read-only
# public endpoints: catalog (tracks/cars) and the rule index.
# `coaching/rules/bundle` excluded: returns 503 in dev (no signing
# secret), and the 503 isn't in the spec by design (signing-secret
# wiring is a deploy-time concern, not a contract).
_NO_AUTH_PATHS = (
    "/healthz",
    "/readyz",
    "/api/v1/catalog/tracks",
    "/api/v1/catalog/tracks/{master_track_id}",
    "/api/v1/catalog/cars",
    "/api/v1/catalog/cars/{master_car_id}",
    "/api/v1/coaching/rules",
)

# Anchor each path so e.g. `/api/v1/coaching/rules` doesn't also
# match `/api/v1/coaching/rules/bundle`.
_NO_AUTH_REGEX = "|".join(f"^{re.escape(p)}$" for p in _NO_AUTH_PATHS)

_schema = schemathesis.openapi.from_asgi(
    "/api/v1/openapi.json", app
).include(path_regex=_NO_AUTH_REGEX)


# `negative_data_rejection` is excluded: it asserts every malformed
# input gets rejected, which is stricter than spec adherence (FastAPI
# silently ignores unknown query params). T015's "zero drift" is
# about declared-vs-actual contract, not "everything invalid 4xxs".
from schemathesis.specs.openapi.checks import negative_data_rejection  # noqa: E402


@_schema.parametrize()
def test_openapi_no_auth_endpoints_match_runtime(
    case: schemathesis.Case,
) -> None:
    """Each fuzzed request must return a status code declared in the
    spec, and the response body must validate against the schema for
    that status code."""
    case.call_and_validate(excluded_checks=[negative_data_rejection])


# -- Part 2: route ↔ spec inventory parity -------------------------


# Internal/non-API routes that aren't part of the public contract
# and shouldn't be expected to appear in the OpenAPI spec.
_NON_CONTRACT_PATHS = frozenset(
    {
        "/api/v1/openapi.json",
        "/api/v1/docs",
        "/api/v1/docs/oauth2-redirect",
        "/api/v1/redoc",
        "/metrics",
    }
)


def _runtime_paths() -> set[str]:
    """All concrete API paths the FastAPI app actually serves
    (with path-parameter placeholders preserved)."""
    paths: set[str] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path in _NON_CONTRACT_PATHS:
            continue
        paths.add(route.path)
    return paths


def _spec_paths(spec: dict[str, Any]) -> set[str]:
    return set(spec.get("paths", {}).keys())


def test_every_runtime_route_appears_in_openapi_spec() -> None:
    """A handler the app answers but the spec doesn't declare =
    drift. The test catches it the moment the diff happens."""
    spec = app.openapi()
    missing = _runtime_paths() - _spec_paths(spec)
    assert not missing, f"runtime routes missing from spec: {sorted(missing)}"


def test_every_spec_path_maps_to_a_runtime_route() -> None:
    """A spec path with no real handler = stale spec / dead code."""
    spec = app.openapi()
    extra = _spec_paths(spec) - _runtime_paths() - _NON_CONTRACT_PATHS
    assert not extra, f"spec paths with no runtime route: {sorted(extra)}"


def test_every_spec_operation_declares_at_least_one_response() -> None:
    """A spec operation with no documented response is a malformed
    contract — clients literally don't know what to expect."""
    spec = app.openapi()
    bad: list[str] = []
    for path, ops in spec.get("paths", {}).items():
        for method, op in ops.items():
            if method.startswith("x-"):
                continue
            responses = op.get("responses", {})
            if not responses:
                bad.append(f"{method.upper()} {path}")
    assert not bad, f"operations without declared responses: {bad}"


def test_openapi_spec_is_serializable_and_well_formed() -> None:
    """Catches spec generation crashes (e.g. circular schema refs,
    pydantic models that don't serialize) before they hit prod."""
    import json

    spec = app.openapi()
    # Round-trip through JSON to mirror what FastAPI ships at runtime.
    json.dumps(spec)
    # Required top-level keys per OpenAPI 3.x.
    for key in ("openapi", "info", "paths"):
        assert key in spec, f"OpenAPI spec missing required key: {key}"


def test_openapi_components_schemas_well_formed() -> None:
    """Every component schema referenced by an operation must exist.
    Catches dangling $refs that schemathesis would happily skip but
    that crash a real OpenAPI client at deserialization time."""
    spec = app.openapi()
    component_schemas = set(
        spec.get("components", {}).get("schemas", {}).keys()
    )
    refs_used: set[str] = set()
    pattern = re.compile(r"#/components/schemas/([A-Za-z0-9_]+)")

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "$ref" and isinstance(v, str):
                    m = pattern.match(v)
                    if m:
                        refs_used.add(m.group(1))
                else:
                    _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(spec.get("paths", {}))
    _walk(spec.get("components", {}))
    dangling = refs_used - component_schemas
    assert not dangling, f"dangling component refs: {sorted(dangling)}"
