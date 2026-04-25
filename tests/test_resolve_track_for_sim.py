"""T144 — Unit tests: track catalog load + lookup by sim/track name.

Composes the two existing layers (sim equivalence and the in-memory
catalog) and asserts an end-to-end "sim provider + sim track id →
canonical track data" path that the ingest pipeline depends on.
"""

from coaching.catalog.equivalence import (
    resolve_track_by_name,
    resolve_track_for_sim,
)
from coaching.catalog.loader import (
    DEFAULT_TRACKS_DIR,
    TrackCatalogEntry,
    get_track_catalog,
    load_track_yaml,
)


def test_track_catalog_loads_at_least_ten_tracks() -> None:
    catalog = get_track_catalog()
    assert len(catalog) >= 10
    for entry in catalog.values():
        assert isinstance(entry, TrackCatalogEntry)


def test_resolve_track_for_sim_returns_full_entry_for_known_pair() -> None:
    # ACC's "spa" should resolve to the Spa-Francorchamps catalog entry.
    entry = resolve_track_for_sim(provider="acc", sim_track_id="spa")
    assert entry is not None
    assert entry.name == "spa-francorchamps"
    assert entry.layout_name == "grand-prix"
    # Sanity: the resolved entry has its sector checkpoints populated.
    assert len(entry.sector_checkpoints) == 2


def test_resolve_track_for_sim_returns_none_for_unknown_sim() -> None:
    assert (
        resolve_track_for_sim(
            provider="nonexistent-sim", sim_track_id="bogus"
        )
        is None
    )


def test_resolve_track_for_sim_returns_none_for_dangling_sim_id() -> None:
    # A real provider with an unmapped sim_track_id.
    assert (
        resolve_track_for_sim(provider="acc", sim_track_id="not_a_real_track")
        is None
    )


def test_resolve_track_by_name_finds_seeded_track() -> None:
    # Human-readable lookup (kebab-case slug) is useful for admin
    # tooling and tests, where master_track_ids are opaque.
    entry = resolve_track_by_name("silverstone")
    assert entry is not None
    assert entry.name == "silverstone"
    # Round-trip: the master_track_id of the resolved entry is in
    # the catalog.
    assert entry.master_track_id in get_track_catalog()


def test_resolve_track_by_name_returns_none_for_unknown_name() -> None:
    assert resolve_track_by_name("not-a-real-circuit") is None


def test_per_track_yaml_files_match_what_catalog_returns() -> None:
    # Loose round-trip: every YAML on disk corresponds to one entry
    # in the cached catalog. Catches a regression where the cache
    # silently diverges from the on-disk source.
    catalog = get_track_catalog()
    on_disk_ids = {
        load_track_yaml(p).master_track_id
        for p in DEFAULT_TRACKS_DIR.glob("*.yaml")
    }
    assert on_disk_ids == set(catalog.keys())
