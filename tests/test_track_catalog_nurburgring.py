"""T128 — Nürburgring VLN (Gesamtstrecke) YAML in the track catalog.

Adds the Nürburgring "Gesamtstrecke" — the combined Nordschleife +
Grand-Prix-Strecke layout used by VLN / NLS endurance racing.
Asserts the loader returns reasonable values for length, sector
boundaries, signature corner names, and start/finish coordinates
inside the Eifel region.
"""

from pathlib import Path

from coaching.catalog.loader import TrackCatalogEntry, load_track_yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
NURBURGRING_YAML = (
    REPO_ROOT / "coaching" / "catalog" / "tracks" / "nurburgring.yaml"
)


def test_nurburgring_yaml_parses_to_track_catalog_entry() -> None:
    entry = load_track_yaml(NURBURGRING_YAML)
    assert isinstance(entry, TrackCatalogEntry)
    assert entry.name == "nurburgring"
    assert entry.layout_name == "vln-gesamtstrecke"


def test_nurburgring_length_matches_gesamtstrecke() -> None:
    # Gesamtstrecke (Nordschleife + GP combined) is ~25.378 km.
    entry = load_track_yaml(NURBURGRING_YAML)
    assert 25_000.0 < entry.length_m < 26_000.0


def test_nurburgring_has_two_sector_checkpoints_strictly_increasing() -> None:
    entry = load_track_yaml(NURBURGRING_YAML)
    assert len(entry.sector_checkpoints) == 2
    distances = [cp.distance_m for cp in entry.sector_checkpoints]
    assert distances == sorted(distances)
    assert all(0 < d < entry.length_m for d in distances)


def test_nurburgring_corner_names_include_signature_nordschleife() -> None:
    # Signature Nordschleife landmarks must be present — sanity-check
    # that the YAML wasn't filled with placeholder data.
    entry = load_track_yaml(NURBURGRING_YAML)
    joined = " ".join(entry.corner_names).lower()
    assert "karussell" in joined
    assert "bergwerk" in joined
    assert "pflanzgarten" in joined


def test_nurburgring_start_finish_is_in_the_eifel() -> None:
    entry = load_track_yaml(NURBURGRING_YAML)
    # GP-Strecke start/finish ~50.335N, 6.947E. Sanity-check ranges.
    assert 50.2 < entry.start_finish_lat < 50.5
    assert 6.8 < entry.start_finish_lon < 7.1
    assert 0.0 <= entry.start_finish_heading_deg < 360.0


def test_nurburgring_master_track_id_differs_from_existing_catalog() -> None:
    # Sim↔real equivalence is per-circuit; Nürburgring must be
    # distinct from every other catalog entry.
    nurburgring = load_track_yaml(NURBURGRING_YAML)
    sebring = load_track_yaml(
        REPO_ROOT / "coaching" / "catalog" / "tracks" / "sebring.yaml"
    )
    silverstone = load_track_yaml(
        REPO_ROOT / "coaching" / "catalog" / "tracks" / "silverstone.yaml"
    )
    assert nurburgring.master_track_id != sebring.master_track_id
    assert nurburgring.master_track_id != silverstone.master_track_id
