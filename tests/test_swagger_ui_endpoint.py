"""T227 — Swagger UI mounted at `/api/v1/docs`.

FastAPI ships Swagger UI as a built-in HTML viewer that loads
the OpenAPI spec; T227 just relocates it under the versioned
API path so it sits next to the spec (T226) and the routes it
documents.

Co-locating spec + UI under `/api/v1/...` means the dev
hitting `https://api.example.com/api/v1/docs` doesn't have to
remember which top-level path serves the human-facing viewer
versus the JSON.
"""

from fastapi.testclient import TestClient

from app.main import app


def test_swagger_ui_renders_at_versioned_path() -> None:
    resp = TestClient(app).get("/api/v1/docs")
    assert resp.status_code == 200
    # Swagger UI's HTML carries this id on the mount-point div —
    # checking for it confirms we got the actual viewer rather
    # than an error page that happened to be 200.
    assert "swagger-ui" in resp.text


def test_swagger_ui_points_at_versioned_openapi_url() -> None:
    """The Swagger HTML must reference our relocated spec
    (`/api/v1/openapi.json`), not the default `/openapi.json`.
    Otherwise the viewer would 404 on schema load."""
    resp = TestClient(app).get("/api/v1/docs")
    assert "/api/v1/openapi.json" in resp.text


def test_default_docs_path_is_disabled() -> None:
    """Default `/docs` must 404 to avoid serving two UIs."""
    resp = TestClient(app).get("/docs")
    assert resp.status_code == 404
