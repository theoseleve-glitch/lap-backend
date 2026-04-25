"""T133 — Charlotte Motor Speedway YAML in the track catalog.

Adds Charlotte Motor Speedway (Concord, NC — NASCAR / IMSA venue).
Defaults to the "Roval" road-course layout used by NASCAR Cup and
iRacing, since that's the configuration that fits a corner/sector
schema. Asserts the loader returns reasonable values for length,
sector boundaries, signature labels, and start/finish coordinates
inside Cabarrus County, NC.
"""

from pathlib import Path

from coaching.catalog.loader import TrackCatalogEntry, load_track_yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CHARLOTTE_YAML = (
    REPO_ROOT / "coaching" / "catalog" / "tracks" / "charlotte.yaml"
)


def test_charlotte_yaml_parses_to_track_catalog_entry() -> None:
    entry = load_track_yaml(CHARLOTTE_YAML)
    assert isinstance(entry, TrackCatalogEntry)
    assert entry.name == "charlotte"
    assert entry.layout_name == "roval"


def test_charlotte_length_matches_roval() -> None:
    # Roval is 2.32 miles = ~3733m.
    entry = load_track_yaml(CHARLOTTE_YAML)
    assert 3500.0 < entry.length_m < 3900.0


def test_charlotte_has_two_sector_checkpoints_strictly_increasing() -> None:
    entry = load_track_yaml(CHARLOTTE_YAML)
    assert len(entry.sector_checkpoints) == 2
    distances = [cp.distance_m for cp in entry.sector_checkpoints]
    assert distances == sorted(distances)
    assert all(0 < d < entry.length_m for d in distances)


def test_charlotte_corner_names_include_roval_landmarks() -> None:
    # Roval signature: chicanes (frontstretch + backstretch),
    # infield section, and the embedded oval turns.
    entry = load_track_yaml(CHARLOTTE_YAML)
    joined = " ".join(entry.corner_names).lower()
    assert "chicane" in joined
    assert "infield" in joined
    assert "oval" in joined


def test_charlotte_start_finish_is_in_cabarrus_county() -> None:
    entry = load_track_yaml(CHARLOTTE_YAML)
    # Concord, NC ~35.353N, -80.683W. Sanity-check ranges.
    assert 35.2 < entry.start_finish_lat < 35.5
    assert -80.8 < entry.start_finish_lon < -80.5
    assert 0.0 <= entry.start_finish_heading_deg < 360.0


def test_charlotte_master_track_id_differs_from_existing_catalog() -> None:
    charlotte = load_track_yaml(CHARLOTTE_YAML)
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
    ledenon = load_track_yaml(
        REPO_ROOT / "coaching" / "catalog" / "tracks" / "ledenon.yaml"
    )
    assert charlotte.master_track_id != sebring.master_track_id
    assert charlotte.master_track_id != silverstone.master_track_id
    assert charlotte.master_track_id != nurburgring.master_track_id
    assert charlotte.master_track_id != rbr.master_track_id
    assert charlotte.master_track_id != laguna.master_track_id
    assert charlotte.master_track_id != lime.master_track_id
    assert charlotte.master_track_id != ledenon.master_track_id
