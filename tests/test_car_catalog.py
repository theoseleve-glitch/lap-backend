"""T138 — Car catalog seed parses.

ADR-014: server owns the car catalog (same pattern as tracks).
This test walks every `coaching/catalog/cars/*.yaml`, parses it
through `load_car_yaml`, and asserts the catalog meets the seed
requirements: at least 15 GT3 cars and at least 3 karting classes,
with unique `master_car_id` values across the whole set.
"""

from pathlib import Path

import pytest

from coaching.catalog.loader import CarCatalogEntry, load_car_yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CARS_DIR = REPO_ROOT / "coaching" / "catalog" / "cars"


def _all_car_yamls() -> list[Path]:
    return sorted(CARS_DIR.glob("*.yaml"))


@pytest.mark.parametrize("path", _all_car_yamls(), ids=lambda p: p.stem)
def test_each_car_yaml_parses(path: Path) -> None:
    entry = load_car_yaml(path)
    assert isinstance(entry, CarCatalogEntry)
    assert entry.name
    assert entry.manufacturer
    assert entry.model
    assert entry.year > 1900
    assert entry.car_class
    assert entry.master_car_id
    assert entry.mass_kg > 0
    assert entry.power_kw > 0
    assert entry.drive_type in {"RWD", "FWD", "AWD"}
    assert entry.aspiration in {"NA", "Turbo", "Supercharged", "Hybrid"}


def test_catalog_has_at_least_15_gt3_cars() -> None:
    gt3_count = sum(
        1
        for path in _all_car_yamls()
        if load_car_yaml(path).car_class == "GT3"
    )
    assert gt3_count >= 15


def test_catalog_has_at_least_three_karting_classes() -> None:
    karting_count = sum(
        1
        for path in _all_car_yamls()
        if load_car_yaml(path).car_class.startswith("Karting")
    )
    assert karting_count >= 3


def test_all_master_car_ids_are_unique() -> None:
    ids = [load_car_yaml(p).master_car_id for p in _all_car_yamls()]
    assert len(ids) == len(set(ids))


def test_gt3_mass_and_power_are_in_a_realistic_band() -> None:
    # GT3 BoP windows: ~1250-1400 kg, ~360-470 kW (~480-630 hp).
    # Loose bounds — the seed shouldn't have a typo'd 13 hp car.
    for path in _all_car_yamls():
        entry = load_car_yaml(path)
        if entry.car_class != "GT3":
            continue
        assert 1200.0 <= entry.mass_kg <= 1450.0
        assert 350.0 <= entry.power_kw <= 480.0


def test_karting_mass_and_power_are_in_a_realistic_band() -> None:
    # Karting (with driver): ~155-200 kg, ~15-45 kW depending on
    # class. KZ shifter is the upper bound; X30/Rotax are lighter.
    for path in _all_car_yamls():
        entry = load_car_yaml(path)
        if not entry.car_class.startswith("Karting"):
            continue
        assert 140.0 <= entry.mass_kg <= 220.0
        assert 10.0 <= entry.power_kw <= 50.0
