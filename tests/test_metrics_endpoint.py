"""T175 — `GET /metrics` exposes the Prometheus exposition format.

Mounted at the root, not under /api/v1, so the standard Prometheus
scrape config (`/metrics` everywhere) works without per-app
relabeling. Returns whatever the default registry has — by default
the prometheus_client process collector ships memory, cpu, fds, etc.
"""

from fastapi.testclient import TestClient

from app.main import app


def test_metrics_endpoint_returns_200() -> None:
    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200


def test_metrics_endpoint_uses_prometheus_content_type() -> None:
    # Prometheus' line protocol: "text/plain; version=...; charset=utf-8".
    # The version part shifts across prometheus_client releases — we
    # only guarantee the prefix.
    client = TestClient(app)
    resp = client.get("/metrics")
    ctype = resp.headers["content-type"]
    assert ctype.startswith("text/plain"), ctype
    assert "version=" in ctype
    assert "charset=utf-8" in ctype


def test_metrics_endpoint_includes_default_python_metrics() -> None:
    # The prometheus_client default registry includes Python GC
    # metrics on every platform (process collector is unix-only and
    # would skip the Windows test runner). Proves the registry is
    # wired through and the exposition format is well-formed.
    client = TestClient(app)
    resp = client.get("/metrics")
    assert "# HELP" in resp.text
    assert "# TYPE" in resp.text
    assert "python_gc_collections_total" in resp.text
