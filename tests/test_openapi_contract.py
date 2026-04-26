"""T229 — Schemathesis contract test against the OpenAPI spec.

Verifies that runtime endpoints conform to the schema FastAPI
generates and exposes at `/api/v1/openapi.json` (T226). Caught
in two ways:

    - Status codes the runtime returns must be declared in the
      spec (otherwise the spec is lying to clients).
    - Response bodies must validate against the documented
      schema for that status code.

Scope: this contract pin runs against `/healthz` — a no-auth
endpoint covered end-to-end by the spec. Wider coverage across
auth-protected routes needs request-side auth setup the
schemathesis fuzzer can't manufacture; deferred to a follow-up
once we want to exercise the full API surface against the
contract.

The point of T229 is "the schemathesis wiring works and runs
green in CI" — not "every endpoint is fuzzed today."
"""

import schemathesis

from app.main import app

# ASGI in-process — no HTTP server needed. The schema URL is
# the relocated path from T226. Limit fuzz scope to the no-auth
# health endpoint via .include() — broader fuzzing needs auth
# bearers schemathesis can't generate from the schema alone.
_schema = schemathesis.openapi.from_asgi(
    "/api/v1/openapi.json",
    app,
).include(path="/healthz")


@_schema.parametrize()
def test_openapi_matches_runtime(case: schemathesis.Case) -> None:
    case.call_and_validate()
