import httpx
from fastapi.testclient import TestClient

from app.main import app

ALLOWED_ORIGIN = "https://app.lap-coach.racing"
DISALLOWED_ORIGIN = "https://evil.example"


def _preflight(origin: str) -> httpx.Response:
    return TestClient(app).options(
        "/healthz",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )


def test_cors_preflight_allows_configured_origin() -> None:
    response = _preflight(ALLOWED_ORIGIN)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN


def test_cors_preflight_rejects_other_origin() -> None:
    response = _preflight(DISALLOWED_ORIGIN)
    assert response.headers.get("access-control-allow-origin") != DISALLOWED_ORIGIN
