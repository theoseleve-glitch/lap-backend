"""T141 — `GET /api/v1/catalog/tracks/:id` detail.

Returns a single track entry by `master_track_id`, including its
sector checkpoints and corner names. 404 for unknown ids.
"""

from fastapi.testclient import TestClient

from app.main import app
from coaching.catalog.loader import get_track_catalog


def test_get_track_detail_returns_known_track() -> None:
    catalog = get_track_catalog()
    [(known_id, known_entry), *_] = catalog.items()
    client = TestClient(app)
    resp = client.get(f"/api/v1/catalog/tracks/{known_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["master_track_id"] == known_id
    assert body["name"] == known_entry.name


def test_get_track_detail_includes_sector_checkpoints() -> None:
    catalog = get_track_catalog()
    [(known_id, known_entry), *_] = catalog.items()
    client = TestClient(app)
    resp = client.get(f"/api/v1/catalog/tracks/{known_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["sector_checkpoints"], list)
    assert len(body["sector_checkpoints"]) == len(known_entry.sector_checkpoints)
    for got, expected in zip(
        body["sector_checkpoints"], known_entry.sector_checkpoints, strict=True
    ):
        assert got["distance_m"] == expected.distance_m
        assert got["name"] == expected.name


def test_get_track_detail_includes_corner_names() -> None:
    catalog = get_track_catalog()
    [(known_id, known_entry), *_] = catalog.items()
    client = TestClient(app)
    resp = client.get(f"/api/v1/catalog/tracks/{known_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["corner_names"] == list(known_entry.corner_names)


def test_get_track_detail_unknown_id_returns_404() -> None:
    client = TestClient(app)
    resp = client.get(
        "/api/v1/catalog/tracks/019dcacb-0000-0000-0000-deadbeefcafe"
    )
    assert resp.status_code == 404


def test_get_track_detail_response_has_all_documented_fields() -> None:
    catalog = get_track_catalog()
    [(known_id, _), *_] = catalog.items()
    client = TestClient(app)
    resp = client.get(f"/api/v1/catalog/tracks/{known_id}")
    assert resp.status_code == 200
    body = resp.json()
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
        assert field in body, f"missing field: {field}"
