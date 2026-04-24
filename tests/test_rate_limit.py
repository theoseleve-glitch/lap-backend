from fastapi.testclient import TestClient

from app.main import app
from app.rate_limit import rate_limit


def test_rate_limit_decorator_preserves_behavior() -> None:
    @rate_limit("5/minute")
    def add(x: int, y: int) -> int:
        return x + y

    assert add(2, 3) == 5


def test_rate_limit_decorator_records_spec() -> None:
    @rate_limit("5/minute")
    def endpoint() -> None:
        return None

    assert endpoint.__rate_limit__ == "5/minute"  # type: ignore[attr-defined]


def test_rate_limit_middleware_is_no_op() -> None:
    client = TestClient(app)
    for _ in range(20):
        response = client.get("/healthz")
        assert response.status_code == 200
