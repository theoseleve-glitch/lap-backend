"""T136 — `get_track_catalog()` reads every YAML in the catalog
directory at first call and returns them keyed by master_track_id.

Production code calls `get_track_catalog()` (no args, cached).
Tests use `load_track_catalog(tracks_dir)` for explicit-directory
loading so they can inject empty / duplicate / malformed inputs
without touching the cached production state.
"""

from pathlib import Path

import pytest

from coaching.catalog.loader import (
    TrackCatalogEntry,
    TrackCatalogParseError,
    get_track_catalog,
    load_track_catalog,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
TRACKS_DIR = REPO_ROOT / "coaching" / "catalog" / "tracks"

EXPECTED_TRACK_NAMES = frozenset(
    {
        "sebring",
        "silverstone",
        "nurburgring",
        "red-bull-ring",
        "laguna-seca",
        "lime-rock",
        "ledenon",
        "charlotte",
        "spa-francorchamps",
        "monza",
    }
)


def test_get_track_catalog_returns_a_non_empty_mapping() -> None:
    catalog = get_track_catalog()
    assert isinstance(catalog, dict)
    assert len(catalog) >= 10


def test_get_track_catalog_keys_are_master_track_ids() -> None:
    catalog = get_track_catalog()
    for key, entry in catalog.items():
        assert isinstance(entry, TrackCatalogEntry)
        assert key == entry.master_track_id


def test_get_track_catalog_includes_every_seeded_track() -> None:
    catalog = get_track_catalog()
    names_loaded = {entry.name for entry in catalog.values()}
    assert names_loaded >= EXPECTED_TRACK_NAMES


def test_get_track_catalog_master_track_ids_are_unique() -> None:
    catalog = get_track_catalog()
    ids_seen = [entry.master_track_id for entry in catalog.values()]
    assert len(ids_seen) == len(set(ids_seen))


def test_get_track_catalog_is_cached_across_calls() -> None:
    # Two calls should return *the same object* (cached) so callers
    # can rely on identity for equality checks.
    first = get_track_catalog()
    second = get_track_catalog()
    assert first is second


def test_load_track_catalog_with_explicit_directory_matches_default() -> None:
    explicit = load_track_catalog(TRACKS_DIR)
    cached = get_track_catalog()
    assert set(explicit.keys()) == set(cached.keys())


def test_load_track_catalog_skips_non_yaml_files(tmp_path: Path) -> None:
    # A README or other stray file in the directory must not break
    # bulk loading or end up in the catalog.
    silver = (TRACKS_DIR / "silverstone.yaml").read_text(encoding="utf-8")
    (tmp_path / "silverstone.yaml").write_text(silver, encoding="utf-8")
    (tmp_path / "README.md").write_text("# notes", encoding="utf-8")
    catalog = load_track_catalog(tmp_path)
    assert len(catalog) == 1
    [entry] = catalog.values()
    assert entry.name == "silverstone"


def test_load_track_catalog_rejects_duplicate_master_track_ids(
    tmp_path: Path,
) -> None:
    silver = (TRACKS_DIR / "silverstone.yaml").read_text(encoding="utf-8")
    (tmp_path / "silverstone.yaml").write_text(silver, encoding="utf-8")
    # Same content under a different filename = same master_track_id.
    (tmp_path / "silverstone-copy.yaml").write_text(silver, encoding="utf-8")
    with pytest.raises(TrackCatalogParseError) as excinfo:
        load_track_catalog(tmp_path)
    assert "duplicate_master_track_id" in str(excinfo.value)


def test_load_track_catalog_empty_directory_returns_empty_mapping(
    tmp_path: Path,
) -> None:
    catalog = load_track_catalog(tmp_path)
    assert catalog == {}
