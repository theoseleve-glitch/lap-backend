"""T129 — Red Bull Ring YAML in the track catalog.

Adds the Red Bull Ring (Spielberg, Austria — F1 / DTM venue,
formerly Österreichring / A1-Ring). Asserts the loader returns
reasonable values for length, sector boundaries, signature corner
names, and start/finish coordinates inside Styria.
"""

from pathlib import Path

from coaching.catalog.loader import TrackCatalogEntry, load_track_yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
RED_BULL_RING_YAML = (
    REPO_ROOT / "coaching" / "catalog" / "tracks" / "red-bull-ring.yaml"
)


def test_red_bull_ring_yaml_parses_to_track_catalog_entry() -> None:
    entry = load_track_yaml(RED_BULL_RING_YAML)
    assert isinstance(entry, TrackCatalogEntry)
    assert entry.name == "red-bull-ring"
    assert entry.layout_name == "grand-prix"


def test_red_bull_ring_length_matches_gp_layout() -> None:
    # Red Bull Ring GP layout is officially 4318m.
    entry = load_track_yaml(RED_BULL_RING_YAML)
    assert 4200.0 < entry.length_m < 4400.0


def test_red_bull_ring_has_two_sector_checkpoints_strictly_increasing() -> None:
    entry = load_track_yaml(RED_BULL_RING_YAML)
    assert len(entry.sector_checkpoints) == 2
    distances = [cp.distance_m for cp in entry.sector_checkpoints]
    assert distances == sorted(distances)
    assert all(0 < d < entry.length_m for d in distances)


def test_red_bull_ring_corner_names_include_signature_corners() -> None:
    # Niki Lauda Kurve (T1) and Remus (T3) are the canonical
    # named corners; Rindt (T9) closes the lap.
    entry = load_track_yaml(RED_BULL_RING_YAML)
    joined = " ".join(entry.corner_names).lower()
    assert "lauda" in joined
    assert "remus" in joined
    assert "rindt" in joined


def test_red_bull_ring_start_finish_is_in_styria() -> None:
    entry = load_track_yaml(RED_BULL_RING_YAML)
    # Spielberg, Styria ~47.22N, 14.76E. Sanity-check ranges.
    assert 47.1 < entry.start_finish_lat < 47.3
    assert 14.7 < entry.start_finish_lon < 14.9
    assert 0.0 <= entry.start_finish_heading_deg < 360.0


def test_red_bull_ring_master_track_id_differs_from_existing_catalog() -> None:
    # Sim↔real equivalence is per-circuit; Red Bull Ring must be
    # distinct from every other catalog entry.
    rbr = load_track_yaml(RED_BULL_RING_YAML)
    sebring = load_track_yaml(
        REPO_ROOT / "coaching" / "catalog" / "tracks" / "sebring.yaml"
    )
    silverstone = load_track_yaml(
        REPO_ROOT / "coaching" / "catalog" / "tracks" / "silverstone.yaml"
    )
    nurburgring = load_track_yaml(
        REPO_ROOT / "coaching" / "catalog" / "tracks" / "nurburgring.yaml"
    )
    assert rbr.master_track_id != sebring.master_track_id
    assert rbr.master_track_id != silverstone.master_track_id
    assert rbr.master_track_id != nurburgring.master_track_id
