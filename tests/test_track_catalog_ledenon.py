"""T132 — Circuit de Lédenon YAML in the track catalog.

Adds Circuit de Lédenon (Lédenon, Gard, Occitanie, France — hilly
3.1km natural-terrain circuit). Asserts the loader returns
reasonable values for length, sector boundaries, signature corner
names, and start/finish coordinates inside southern France.
"""

from pathlib import Path

from coaching.catalog.loader import TrackCatalogEntry, load_track_yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
LEDENON_YAML = (
    REPO_ROOT / "coaching" / "catalog" / "tracks" / "ledenon.yaml"
)


def test_ledenon_yaml_parses_to_track_catalog_entry() -> None:
    entry = load_track_yaml(LEDENON_YAML)
    assert isinstance(entry, TrackCatalogEntry)
    assert entry.name == "ledenon"
    assert entry.layout_name == "full-course"


def test_ledenon_length_matches_full_course() -> None:
    # Full course is ~3.151 km.
    entry = load_track_yaml(LEDENON_YAML)
    assert 3000.0 < entry.length_m < 3300.0


def test_ledenon_has_two_sector_checkpoints_strictly_increasing() -> None:
    entry = load_track_yaml(LEDENON_YAML)
    assert len(entry.sector_checkpoints) == 2
    distances = [cp.distance_m for cp in entry.sector_checkpoints]
    assert distances == sorted(distances)
    assert all(0 < d < entry.length_m for d in distances)


def test_ledenon_corner_names_include_signature_corners() -> None:
    # Le Tourniquet, Le Saut, and the Combe complex are canonical
    # Lédenon landmarks.
    entry = load_track_yaml(LEDENON_YAML)
    joined = " ".join(entry.corner_names).lower()
    assert "tourniquet" in joined
    assert "saut" in joined
    assert "combe" in joined


def test_ledenon_start_finish_is_in_southern_france() -> None:
    entry = load_track_yaml(LEDENON_YAML)
    # Lédenon, Gard ~43.93N, 4.50E. Sanity-check ranges.
    assert 43.8 < entry.start_finish_lat < 44.05
    assert 4.4 < entry.start_finish_lon < 4.6
    assert 0.0 <= entry.start_finish_heading_deg < 360.0


def test_ledenon_master_track_id_differs_from_existing_catalog() -> None:
    ledenon = load_track_yaml(LEDENON_YAML)
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
    lime = load_track_yaml(
        REPO_ROOT / "coaching" / "catalog" / "tracks" / "lime-rock.yaml"
    )
    assert ledenon.master_track_id != sebring.master_track_id
    assert ledenon.master_track_id != silverstone.master_track_id
    assert ledenon.master_track_id != nurburgring.master_track_id
    assert ledenon.master_track_id != rbr.master_track_id
    assert ledenon.master_track_id != laguna.master_track_id
    assert ledenon.master_track_id != lime.master_track_id
