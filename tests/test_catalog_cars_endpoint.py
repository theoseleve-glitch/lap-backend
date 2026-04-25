"""T142 — `GET /api/v1/catalog/cars` list.

Returns the server-curated car catalog. Unauthenticated like the
track catalog (non-sensitive reference data, needed before login).
Paginated by limit/offset for parity with the tracks endpoint.
"""

from typing import Any

from fastapi.testclient import TestClient

from app.main import app


def test_get_catalog_cars_returns_200_and_a_list() -> None:
    client = TestClient(app)
    resp = client.get("/api/v1/catalog/cars")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict)
    assert "items" in body
    assert isinstance(body["items"], list)


def test_get_catalog_cars_returns_at_least_fifteen_cars() -> None:
    # T138 seeded 15+ GT3s + 3 karting classes = 18 entries.
    client = TestClient(app)
    resp = client.get("/api/v1/catalog/cars?limit=100")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 15
    assert len(body["items"]) >= 15


def test_get_catalog_cars_each_item_has_expected_fields() -> None:
    client = TestClient(app)
    resp = client.get("/api/v1/catalog/cars?limit=1")
    assert resp.status_code == 200
    [item] = resp.json()["items"]
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
        assert field in item, f"missing field: {field}"


def test_get_catalog_cars_respects_limit_query_param() -> None:
    client = TestClient(app)
    resp = client.get("/api/v1/catalog/cars?limit=4")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 4
    assert body["limit"] == 4


def test_get_catalog_cars_respects_offset_query_param() -> None:
    client = TestClient(app)
    page1 = client.get("/api/v1/catalog/cars?limit=5&offset=0").json()
    page2 = client.get("/api/v1/catalog/cars?limit=5&offset=5").json()
    page1_ids = {item["master_car_id"] for item in page1["items"]}
    page2_ids = {item["master_car_id"] for item in page2["items"]}
    assert page1_ids.isdisjoint(page2_ids)
    assert page2["offset"] == 5


def test_get_catalog_cars_total_is_consistent_across_pages() -> None:
    client = TestClient(app)
    page1 = client.get("/api/v1/catalog/cars?limit=5&offset=0").json()
    page2 = client.get("/api/v1/catalog/cars?limit=5&offset=5").json()
    assert page1["total"] == page2["total"]


def test_get_catalog_cars_negative_offset_rejected() -> None:
    client = TestClient(app)
    resp = client.get("/api/v1/catalog/cars?offset=-1")
    assert resp.status_code == 422


def test_get_catalog_cars_returns_full_catalog_when_unbounded() -> None:
    client = TestClient(app)
    resp = client.get("/api/v1/catalog/cars?limit=200")
    assert resp.status_code == 200
    body: dict[str, Any] = resp.json()
    assert len(body["items"]) == body["total"]
