"""T140 — `GET /api/v1/catalog/tracks` paginated list.

Returns the server-curated track catalog. Unauthenticated: catalog
content is non-sensitive reference data clients need before login
(e.g., "select your track" UI in registration). Pagination via
?limit=&offset=.
"""

from typing import Any

from fastapi.testclient import TestClient

from app.main import app


def test_get_catalog_tracks_returns_200_and_a_list() -> None:
    client = TestClient(app)
    resp = client.get("/api/v1/catalog/tracks")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict)
    assert "items" in body
    assert isinstance(body["items"], list)


def test_get_catalog_tracks_returns_at_least_ten_tracks() -> None:
    client = TestClient(app)
    resp = client.get("/api/v1/catalog/tracks?limit=100")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 10
    assert len(body["items"]) >= 10


def test_get_catalog_tracks_each_item_has_expected_fields() -> None:
    client = TestClient(app)
    resp = client.get("/api/v1/catalog/tracks?limit=1")
    assert resp.status_code == 200
    [item] = resp.json()["items"]
    for field in (
        "master_track_id",
        "name",
        "layout_name",
        "length_m",
        "start_finish_lat",
        "start_finish_lon",
        "start_finish_heading_deg",
        "sector_checkpoints",
        "corner_names",
    ):
        assert field in item, f"missing field: {field}"


def test_get_catalog_tracks_respects_limit_query_param() -> None:
    client = TestClient(app)
    resp = client.get("/api/v1/catalog/tracks?limit=3")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 3
    assert body["limit"] == 3


def test_get_catalog_tracks_respects_offset_query_param() -> None:
    client = TestClient(app)
    page1 = client.get("/api/v1/catalog/tracks?limit=5&offset=0").json()
    page2 = client.get("/api/v1/catalog/tracks?limit=5&offset=5").json()
    page1_ids = {item["master_track_id"] for item in page1["items"]}
    page2_ids = {item["master_track_id"] for item in page2["items"]}
    assert page1_ids.isdisjoint(page2_ids)
    assert page2["offset"] == 5


def test_get_catalog_tracks_total_is_consistent_across_pages() -> None:
    client = TestClient(app)
    page1 = client.get("/api/v1/catalog/tracks?limit=5&offset=0").json()
    page2 = client.get("/api/v1/catalog/tracks?limit=5&offset=5").json()
    assert page1["total"] == page2["total"]


def test_get_catalog_tracks_clamps_excessive_limit() -> None:
    # An absurdly large limit should not blow up; either clamped
    # silently or rejected with 422 — both are acceptable behaviour.
    client = TestClient(app)
    resp = client.get("/api/v1/catalog/tracks?limit=99999")
    assert resp.status_code in (200, 422)
    if resp.status_code == 200:
        body: dict[str, Any] = resp.json()
        # Whatever the cap is, it shouldn't return more than the
        # whole catalog.
        assert len(body["items"]) == body["total"]


def test_get_catalog_tracks_negative_offset_rejected() -> None:
    client = TestClient(app)
    resp = client.get("/api/v1/catalog/tracks?offset=-1")
    assert resp.status_code == 422
