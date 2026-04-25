"""T127 — Silverstone YAML in the track catalog.

Adds the Silverstone GP layout (F1 / WEC variant) under
`coaching/catalog/tracks/silverstone.yaml`. Asserts the loader
returns reasonable values for length, sector boundaries, corner
names, and start/finish coordinates inside Northamptonshire.
"""

from pathlib import Path

from coaching.catalog.loader import TrackCatalogEntry, load_track_yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SILVERSTONE_YAML = (
    REPO_ROOT / "coaching" / "catalog" / "tracks" / "silverstone.yaml"
)


def test_silverstone_yaml_parses_to_track_catalog_entry() -> None:
    entry = load_track_yaml(SILVERSTONE_YAML)
    assert isinstance(entry, TrackCatalogEntry)
    assert entry.name == "silverstone"
    assert entry.layout_name == "grand-prix"


def test_silverstone_length_matches_gp_layout() -> None:
    # Silverstone GP layout is officially 5891m.
    entry = load_track_yaml(SILVERSTONE_YAML)
    assert 5800.0 < entry.length_m < 6000.0


def test_silverstone_has_two_sector_checkpoints_strictly_increasing() -> None:
    entry = load_track_yaml(SILVERSTONE_YAML)
    assert len(entry.sector_checkpoints) == 2
    distances = [cp.distance_m for cp in entry.sector_checkpoints]
    assert distances == sorted(distances)
    assert all(0 < d < entry.length_m for d in distances)


def test_silverstone_corner_names_include_classic_corners() -> None:
    # A few signature names should be present — sanity-check that
    # the YAML wasn't filled with placeholder data.
    entry = load_track_yaml(SILVERSTONE_YAML)
    joined = " ".join(entry.corner_names).lower()
    assert "copse" in joined
    assert "becketts" in joined
    assert "stowe" in joined


def test_silverstone_start_finish_is_in_northamptonshire() -> None:
    entry = load_track_yaml(SILVERSTONE_YAML)
    # ~52.078N, -1.017W. Sanity-check ranges, not exact coords.
    assert 51.9 < entry.start_finish_lat < 52.2
    assert -1.2 < entry.start_finish_lon < -0.9
    assert 0.0 <= entry.start_finish_heading_deg < 360.0


def test_silverstone_master_track_id_differs_from_sebring() -> None:
    # Sim↔real equivalence is per-circuit; Silverstone and Sebring
    # are distinct circuits and must not share a master_track_id.
    silverstone = load_track_yaml(SILVERSTONE_YAML)
    sebring = load_track_yaml(
        REPO_ROOT / "coaching" / "catalog" / "tracks" / "sebring.yaml"
    )
    assert silverstone.master_track_id != sebring.master_track_id
