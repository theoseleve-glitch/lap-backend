from fastapi.testclient import TestClient

from app.main import app


def test_healthz_returns_200() -> None:
    response = TestClient(app).get("/healthz")
    assert response.status_code == 200
