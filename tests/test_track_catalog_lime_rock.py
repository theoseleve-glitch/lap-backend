"""T131 — Lime Rock Park YAML in the track catalog.

Adds Lime Rock Park (Lakeville, Connecticut — IMSA / Trans-Am
venue, classic short natural-terrain road course). Asserts the
loader returns reasonable values for length, sector boundaries,
signature corner names, and start/finish coordinates inside
Litchfield County, CT.
"""

from pathlib import Path

from coaching.catalog.loader import TrackCatalogEntry, load_track_yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
LIME_ROCK_YAML = (
    REPO_ROOT / "coaching" / "catalog" / "tracks" / "lime-rock.yaml"
)


def test_lime_rock_yaml_parses_to_track_catalog_entry() -> None:
    entry = load_track_yaml(LIME_ROCK_YAML)
    assert isinstance(entry, TrackCatalogEntry)
    assert entry.name == "lime-rock"
    assert entry.layout_name == "classic"


def test_lime_rock_length_matches_classic_layout() -> None:
    # Classic (no-chicane) layout is ~2459m (1.527 miles).
    entry = load_track_yaml(LIME_ROCK_YAML)
    assert 2300.0 < entry.length_m < 2600.0


def test_lime_rock_has_two_sector_checkpoints_strictly_increasing() -> None:
    entry = load_track_yaml(LIME_ROCK_YAML)
    assert len(entry.sector_checkpoints) == 2
    distances = [cp.distance_m for cp in entry.sector_checkpoints]
    assert distances == sorted(distances)
    assert all(0 < d < entry.length_m for d in distances)


def test_lime_rock_corner_names_include_signature_corners() -> None:
    # Big Bend (T1), West Bend, and Diving Turn are canonical
    # Lime Rock landmarks.
    entry = load_track_yaml(LIME_ROCK_YAML)
    joined = " ".join(entry.corner_names).lower()
    assert "big bend" in joined
    assert "west bend" in joined
    assert "diving" in joined


def test_lime_rock_start_finish_is_in_litchfield_county() -> None:
    entry = load_track_yaml(LIME_ROCK_YAML)
    # Lakeville, CT ~41.928N, -73.385W. Sanity-check ranges.
    assert 41.85 < entry.start_finish_lat < 42.0
    assert -73.45 < entry.start_finish_lon < -73.30
    assert 0.0 <= entry.start_finish_heading_deg < 360.0


def test_lime_rock_master_track_id_differs_from_existing_catalog() -> None:
    lime = load_track_yaml(LIME_ROCK_YAML)
    sebring = load_track_yaml(
        REPO_ROOT / "coaching" / "catalog" / "tracks" / "sebring.yaml"
    )
    silverstone = load_track_yaml(
        REPO_ROOT / "coaching" / "catalog" / "tracks" / "silverstone.yaml"
    )
    nurburgring = load_track_yaml(
        REPO_ROOT / "coaching" / "catalog" / "tracks" / "nurburgring.yaml"
    )
    rbr = load_track_yaml(
        REPO_ROOT / "coaching" / "catalog" / "tracks" / "red-bull-ring.yaml"
    )
    laguna = load_track_yaml(
        REPO_ROOT / "coaching" / "catalog" / "tracks" / "laguna-seca.yaml"
    )
    assert lime.master_track_id != sebring.master_track_id
    assert lime.master_track_id != silverstone.master_track_id
    assert lime.master_track_id != nurburgring.master_track_id
    assert lime.master_track_id != rbr.master_track_id
    assert lime.master_track_id != laguna.master_track_id
