"""T130 — Laguna Seca YAML in the track catalog.

Adds WeatherTech Raceway Laguna Seca (Salinas, California — IMSA /
MotoGP venue, formerly Mazda Raceway). Asserts the loader returns
reasonable values for length, sector boundaries, signature corner
names, and start/finish coordinates inside Monterey County, CA.
"""

from pathlib import Path

from coaching.catalog.loader import TrackCatalogEntry, load_track_yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
LAGUNA_SECA_YAML = (
    REPO_ROOT / "coaching" / "catalog" / "tracks" / "laguna-seca.yaml"
)


def test_laguna_seca_yaml_parses_to_track_catalog_entry() -> None:
    entry = load_track_yaml(LAGUNA_SECA_YAML)
    assert isinstance(entry, TrackCatalogEntry)
    assert entry.name == "laguna-seca"
    assert entry.layout_name == "full-course"


def test_laguna_seca_length_matches_full_course() -> None:
    # Full course is officially 3602m (2.238 miles).
    entry = load_track_yaml(LAGUNA_SECA_YAML)
    assert 3500.0 < entry.length_m < 3700.0


def test_laguna_seca_has_two_sector_checkpoints_strictly_increasing() -> None:
    entry = load_track_yaml(LAGUNA_SECA_YAML)
    assert len(entry.sector_checkpoints) == 2
    distances = [cp.distance_m for cp in entry.sector_checkpoints]
    assert distances == sorted(distances)
    assert all(0 < d < entry.length_m for d in distances)


def test_laguna_seca_corner_names_include_signature_corners() -> None:
    # The Corkscrew (T8/T8a) and Rainey Curve (T9) are canonical
    # Laguna Seca landmarks; Andretti Hairpin (T2) closes T1.
    entry = load_track_yaml(LAGUNA_SECA_YAML)
    joined = " ".join(entry.corner_names).lower()
    assert "corkscrew" in joined
    assert "rainey" in joined
    assert "andretti" in joined


def test_laguna_seca_start_finish_is_in_monterey_county() -> None:
    entry = load_track_yaml(LAGUNA_SECA_YAML)
    # Salinas, CA ~36.585N, -121.753W. Sanity-check ranges.
    assert 36.5 < entry.start_finish_lat < 36.7
    assert -121.85 < entry.start_finish_lon < -121.65
    assert 0.0 <= entry.start_finish_heading_deg < 360.0


def test_laguna_seca_master_track_id_differs_from_existing_catalog() -> None:
    laguna = load_track_yaml(LAGUNA_SECA_YAML)
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
    assert laguna.master_track_id != sebring.master_track_id
    assert laguna.master_track_id != silverstone.master_track_id
    assert laguna.master_track_id != nurburgring.master_track_id
    assert laguna.master_track_id != rbr.master_track_id
