"""T226 — OpenAPI spec exposed at `/api/v1/openapi.json`.

FastAPI generates the spec automatically; T226 just relocates
it from the default `/openapi.json` to a versioned path under
`/api/v1/`. This way the spec lives next to the routes it
describes and clients can hit a single base URL for both.

T227 (Swagger UI) and T228 (ReDoc) are the human-facing
viewers; they read from this URL.
"""

from fastapi.testclient import TestClient

from app.main import app


def test_openapi_json_served_at_versioned_path() -> None:
    resp = TestClient(app).get("/api/v1/openapi.json")
    assert resp.status_code == 200
    body = resp.json()
    # The presence of the `openapi` field is the canonical way
    # to identify a valid OpenAPI document — every spec must
    # carry the version string at the root.
    assert "openapi" in body
    assert body["openapi"].startswith("3.")


def test_openapi_json_lists_real_route() -> None:
    """Sanity: the spec actually contains our routes — confirms
    we moved the URL but didn't accidentally clear the schema."""
    resp = TestClient(app).get("/api/v1/openapi.json")
    paths = resp.json()["paths"]
    assert "/healthz" in paths


def test_default_openapi_path_is_disabled() -> None:
    """Once we relocate, the default `/openapi.json` must 404 —
    otherwise we'd be serving the same spec at two URLs and
    creating a stale-cache risk."""
    resp = TestClient(app).get("/openapi.json")
    assert resp.status_code == 404
