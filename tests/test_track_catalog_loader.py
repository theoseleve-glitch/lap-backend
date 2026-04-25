"""T126 — Track catalog YAML loader.

`coaching/catalog/tracks/sebring.yaml` is the canonical seed for
Sebring International. The loader returns a frozen
`TrackCatalogEntry` dataclass with the start/finish coords, sector
checkpoint distances, and corner names — the same shape the
`track_layouts` ORM table will be hydrated from at startup
(rich-shape, not the lap-detection-only `domain.track_layout`).
"""

from pathlib import Path

import pytest

from coaching.catalog.loader import (
    SectorCheckpoint,
    TrackCatalogEntry,
    TrackCatalogParseError,
    load_track_yaml,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SEBRING_YAML = REPO_ROOT / "coaching" / "catalog" / "tracks" / "sebring.yaml"


def test_loads_sebring_returns_track_catalog_entry() -> None:
    entry = load_track_yaml(SEBRING_YAML)
    assert isinstance(entry, TrackCatalogEntry)
    assert entry.name == "sebring"
    assert entry.layout_name == "international-12h"
    # Length of the 12h international layout ~6019m.
    assert 6000.0 < entry.length_m < 6100.0


def test_sebring_carries_two_sector_checkpoints_strictly_increasing() -> None:
    entry = load_track_yaml(SEBRING_YAML)
    # 3 sectors → 2 boundary checkpoints.
    assert len(entry.sector_checkpoints) == 2
    distances = [cp.distance_m for cp in entry.sector_checkpoints]
    assert distances == sorted(distances)
    assert all(0 < d < entry.length_m for d in distances)


def test_sebring_corner_names_are_non_empty() -> None:
    entry = load_track_yaml(SEBRING_YAML)
    # Sebring International has 17 official corners.
    assert len(entry.corner_names) >= 10
    assert all(isinstance(n, str) and n.strip() for n in entry.corner_names)


def test_sebring_start_finish_is_in_florida() -> None:
    entry = load_track_yaml(SEBRING_YAML)
    # Sebring is at ~27.45N, 82.50W. Sanity-check the order of
    # magnitude — protects against lat/lon being swapped.
    assert 27.0 < entry.start_finish_lat < 28.0
    assert -83.0 < entry.start_finish_lon < -82.0
    assert 0.0 <= entry.start_finish_heading_deg < 360.0


def test_sector_checkpoint_is_frozen_dataclass() -> None:
    cp = SectorCheckpoint(distance_m=1830.0, name="S1 end (T7)")
    assert cp.distance_m == 1830.0
    with pytest.raises(Exception, match="(?i)(frozen|immutable|cannot)"):
        cp.distance_m = 1900.0  # type: ignore[misc]


def test_loader_raises_on_invalid_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "broken.yaml"
    bad.write_text(":\n  - not\n   well: indented", encoding="utf-8")
    with pytest.raises(TrackCatalogParseError):
        load_track_yaml(bad)


def test_loader_raises_on_non_dict_top_level(tmp_path: Path) -> None:
    bad = tmp_path / "list_top.yaml"
    bad.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(TrackCatalogParseError, match="(?i)dict"):
        load_track_yaml(bad)


def test_loader_raises_on_missing_required_field(tmp_path: Path) -> None:
    bad = tmp_path / "missing.yaml"
    bad.write_text(
        "name: x\nlayout_name: y\n",
        encoding="utf-8",
    )
    with pytest.raises(TrackCatalogParseError, match="(?i)missing"):
        load_track_yaml(bad)


def test_loader_raises_on_incomplete_start_finish(tmp_path: Path) -> None:
    bad = tmp_path / "no_heading.yaml"
    bad.write_text(
        """
source_provider: test
name: x
layout_name: y
length_m: 1000
master_track_id: 019dcacb-7000-7000-8000-000000000001
start_finish:
  lat: 27.45
  lon: -82.5
sector_checkpoints: []
corner_names: []
""",
        encoding="utf-8",
    )
    with pytest.raises(TrackCatalogParseError, match="(?i)start_finish"):
        load_track_yaml(bad)


def test_master_track_id_is_a_string_not_uuid_object() -> None:
    # Loader keeps the value as a string — UUID parsing is left to
    # consumers so the catalog file format stays YAML-native and
    # validation can happen at the ORM boundary.
    entry = load_track_yaml(SEBRING_YAML)
    assert isinstance(entry.master_track_id, str)
    assert len(entry.master_track_id) == 36  # 8-4-4-4-12 dash format
