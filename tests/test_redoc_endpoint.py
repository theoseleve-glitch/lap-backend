"""T228 — ReDoc viewer mounted at `/api/v1/redoc`.

ReDoc is the second built-in OpenAPI viewer FastAPI ships
(alongside Swagger UI from T227). Different audiences prefer
different presentations: Swagger UI is interactive (try-it-now),
ReDoc is read-only and renders cleaner long-form prose. We
expose both under the versioned API path for consistency.
"""

from fastapi.testclient import TestClient

from app.main import app


def test_redoc_renders_at_versioned_path() -> None:
    resp = TestClient(app).get("/api/v1/redoc")
    assert resp.status_code == 200
    # ReDoc's HTML mounts a custom element with this tag —
    # confirms the actual viewer rendered, not a fallthrough page.
    assert "redoc" in resp.text.lower()


def test_redoc_points_at_versioned_openapi_url() -> None:
    """ReDoc's HTML must reference our relocated spec."""
    resp = TestClient(app).get("/api/v1/redoc")
    assert "/api/v1/openapi.json" in resp.text


def test_default_redoc_path_is_disabled() -> None:
    """Default `/redoc` must 404 — same one-spec-one-URL posture."""
    resp = TestClient(app).get("/redoc")
    assert resp.status_code == 404
