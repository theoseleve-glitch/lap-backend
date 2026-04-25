"""T139 — Sim↔real car equivalence table seeded.

ADR-014: same admin-curated pattern as track equivalence (T137).
Maps sim-specific car identifiers (iRacing car_id, ACC car slug,
AC mod folder name…) to the canonical `master_car_id` defined by
the YAMLs in `coaching/catalog/cars/`. Same physical car, multiple
sim representations, one master id.
"""

from pathlib import Path

import pytest

from coaching.catalog.equivalence import (
    SimCarKey,
    get_sim_car_equivalence,
    load_sim_car_equivalence,
    lookup_master_car_id,
)
from coaching.catalog.loader import (
    DEFAULT_CARS_DIR,
    TrackCatalogParseError,
    load_car_yaml,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EQUIVALENCE_YAML = (
    REPO_ROOT / "coaching" / "catalog" / "sim_car_equivalence.yaml"
)


def _all_known_master_car_ids() -> set[str]:
    return {
        load_car_yaml(p).master_car_id
        for p in DEFAULT_CARS_DIR.glob("*.yaml")
    }


def test_equivalence_yaml_exists() -> None:
    assert EQUIVALENCE_YAML.is_file()


def test_load_sim_car_equivalence_returns_a_mapping() -> None:
    table = load_sim_car_equivalence(EQUIVALENCE_YAML)
    assert isinstance(table, dict)
    assert len(table) >= 10


def test_equivalence_keys_are_provider_sim_id_tuples() -> None:
    table = load_sim_car_equivalence(EQUIVALENCE_YAML)
    for key in table:
        assert isinstance(key, SimCarKey)
        assert key.provider
        assert key.sim_car_id


def test_equivalence_values_reference_real_catalog_entries() -> None:
    known = _all_known_master_car_ids()
    table = load_sim_car_equivalence(EQUIVALENCE_YAML)
    for master_id in table.values():
        assert master_id in known


def test_lookup_master_car_id_finds_known_pairs() -> None:
    table = load_sim_car_equivalence(EQUIVALENCE_YAML)
    [(key, expected_master_id), *_] = table.items()
    got = lookup_master_car_id(
        provider=key.provider,
        sim_car_id=key.sim_car_id,
    )
    assert got == expected_master_id


def test_lookup_master_car_id_returns_none_for_unknown_pair() -> None:
    got = lookup_master_car_id(
        provider="nonexistent-sim",
        sim_car_id="bogus_car_42",
    )
    assert got is None


def test_get_sim_car_equivalence_is_cached() -> None:
    first = get_sim_car_equivalence()
    second = get_sim_car_equivalence()
    assert first is second


def test_load_rejects_dangling_master_car_ids(tmp_path: Path) -> None:
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text(
        "pairs:\n"
        "  - provider: iracing\n"
        "    sim_car_id: '999999'\n"
        "    master_car_id: 019dcacb-0000-0000-0000-deadbeefcafe\n",
        encoding="utf-8",
    )
    with pytest.raises(TrackCatalogParseError) as excinfo:
        load_sim_car_equivalence(bad_yaml, validate_against_catalog=True)
    assert "unknown_master_car_id" in str(excinfo.value)


def test_multiple_sim_entries_can_resolve_to_same_master_car_id() -> None:
    # The whole point of the equivalence table: iRacing's Porsche
    # 911 GT3 R and ACC's Porsche 991ii GT3 R should both resolve
    # to the *same* master_car_id (one physical car, two sim slugs).
    table = load_sim_car_equivalence(EQUIVALENCE_YAML)
    by_master_id: dict[str, list[SimCarKey]] = {}
    for key, master_id in table.items():
        by_master_id.setdefault(master_id, []).append(key)
    assert any(len(keys) >= 2 for keys in by_master_id.values()), (
        "Equivalence table is meant to demonstrate cross-sim mappings; "
        "expected at least one master_car_id with ≥2 sim entries."
    )
