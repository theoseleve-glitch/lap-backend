"""T137 — Sim↔real track equivalence table seeded (10 pairs).

ADR-014: server owns an admin-curated mapping from sim-specific
track identifiers (iRacing track_id, ACC slug, AC folder name…)
to the canonical `master_track_id`. Same circuit, multiple sims,
one master id.
"""

from pathlib import Path

import pytest

from coaching.catalog.equivalence import (
    SimTrackKey,
    get_sim_track_equivalence,
    load_sim_track_equivalence,
    lookup_master_track_id,
)
from coaching.catalog.loader import get_track_catalog

REPO_ROOT = Path(__file__).resolve().parents[1]
EQUIVALENCE_YAML = (
    REPO_ROOT / "coaching" / "catalog" / "sim_track_equivalence.yaml"
)


def test_equivalence_yaml_exists() -> None:
    assert EQUIVALENCE_YAML.is_file()


def test_load_sim_track_equivalence_returns_a_mapping() -> None:
    table = load_sim_track_equivalence(EQUIVALENCE_YAML)
    assert isinstance(table, dict)
    assert len(table) >= 10


def test_equivalence_keys_are_provider_sim_id_tuples() -> None:
    table = load_sim_track_equivalence(EQUIVALENCE_YAML)
    for key in table:
        assert isinstance(key, SimTrackKey)
        assert key.provider
        assert key.sim_track_id


def test_equivalence_values_reference_real_catalog_entries() -> None:
    # Every master_track_id in the equivalence table must point at
    # an entry that actually exists in the catalog.
    catalog = get_track_catalog()
    table = load_sim_track_equivalence(EQUIVALENCE_YAML)
    catalog_ids = set(catalog.keys())
    for master_id in table.values():
        assert master_id in catalog_ids


def test_equivalence_covers_at_least_one_entry_per_seeded_track() -> None:
    # 10 catalog tracks, 10 pairs minimum — every catalog entry
    # should have at least one sim mapping.
    catalog = get_track_catalog()
    table = load_sim_track_equivalence(EQUIVALENCE_YAML)
    mapped_master_ids = set(table.values())
    assert mapped_master_ids == set(catalog.keys())


def test_lookup_master_track_id_finds_known_pairs() -> None:
    table = load_sim_track_equivalence(EQUIVALENCE_YAML)
    # Pick any seeded pair and round-trip it.
    [(key, expected_master_id), *_] = table.items()
    got = lookup_master_track_id(
        provider=key.provider,
        sim_track_id=key.sim_track_id,
    )
    assert got == expected_master_id


def test_lookup_master_track_id_returns_none_for_unknown_pair() -> None:
    got = lookup_master_track_id(
        provider="nonexistent-sim",
        sim_track_id="bogus_track_42",
    )
    assert got is None


def test_get_sim_track_equivalence_is_cached() -> None:
    first = get_sim_track_equivalence()
    second = get_sim_track_equivalence()
    assert first is second


def test_load_rejects_dangling_master_track_ids(tmp_path: Path) -> None:
    # Master ids that don't exist in the catalog must be rejected
    # at load time so a typo is caught before runtime.
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text(
        "pairs:\n"
        "  - provider: iracing\n"
        "    sim_track_id: '999999'\n"
        "    master_track_id: 019dcacb-0000-0000-0000-deadbeefdead\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        load_sim_track_equivalence(bad_yaml, validate_against_catalog=True)
    assert "unknown_master_track_id" in str(excinfo.value)
