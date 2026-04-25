"""T143 — `GET /api/v1/catalog/cars/:id` detail.

Returns a single car entry by `master_car_id`, including specs.
404 for unknown ids.
"""

from fastapi.testclient import TestClient

from app.main import app
from coaching.catalog.loader import get_car_catalog


def test_get_car_detail_returns_known_car() -> None:
    catalog = get_car_catalog()
    [(known_id, known_entry), *_] = catalog.items()
    client = TestClient(app)
    resp = client.get(f"/api/v1/catalog/cars/{known_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["master_car_id"] == known_id
    assert body["name"] == known_entry.name
    assert body["manufacturer"] == known_entry.manufacturer


def test_get_car_detail_includes_specs() -> None:
    catalog = get_car_catalog()
    [(known_id, known_entry), *_] = catalog.items()
    client = TestClient(app)
    resp = client.get(f"/api/v1/catalog/cars/{known_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mass_kg"] == known_entry.mass_kg
    assert body["power_kw"] == known_entry.power_kw
    assert body["drive_type"] == known_entry.drive_type
    assert body["aspiration"] == known_entry.aspiration
    assert body["car_class"] == known_entry.car_class
    assert body["year"] == known_entry.year


def test_get_car_detail_unknown_id_returns_404() -> None:
    client = TestClient(app)
    resp = client.get(
        "/api/v1/catalog/cars/019dcacb-0000-0000-0000-deadbeefcafe"
    )
    assert resp.status_code == 404


def test_get_car_detail_response_has_all_documented_fields() -> None:
    catalog = get_car_catalog()
    [(known_id, _), *_] = catalog.items()
    client = TestClient(app)
    resp = client.get(f"/api/v1/catalog/cars/{known_id}")
    assert resp.status_code == 200
    body = resp.json()
    for field in (
        "master_car_id",
        "name",
        "manufacturer",
        "model",
        "year",
        "car_class",
        "mass_kg",
        "power_kw",
        "drive_type",
        "aspiration",
    ):
        assert field in body, f"missing field: {field}"
