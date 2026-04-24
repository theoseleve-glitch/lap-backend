import io
import json
import uuid
from contextlib import redirect_stdout

from fastapi.testclient import TestClient

from app.main import app

HEADER = "x-request-id"


def test_request_id_present_in_response() -> None:
    response = TestClient(app).get("/healthz")
    value = response.headers.get(HEADER)
    assert value is not None
    uuid.UUID(value)


def test_request_id_echoed_from_client_header() -> None:
    client_id = "trace-7c9e6679-7425"
    response = TestClient(app).get("/healthz", headers={HEADER: client_id})
    assert response.headers[HEADER] == client_id


def test_request_id_appears_in_json_log() -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        TestClient(app).get("/healthz")

    for line in buf.getvalue().splitlines():
        try:
            data = json.loads(line.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if data.get("event") == "http_request":
            assert "request_id" in data
            uuid.UUID(data["request_id"])
            return
    raise AssertionError("no http_request log with request_id")
