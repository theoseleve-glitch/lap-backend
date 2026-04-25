"""T135 — Autodromo Nazionale Monza YAML in the track catalog.

Adds Monza's Grand-Prix layout (F1 / WEC venue, the "Temple of
Speed"). Asserts the loader returns reasonable values for length,
sector boundaries, signature corner names (Lesmo, Ascari,
Parabolica), and start/finish coordinates inside Lombardy, Italy.
"""

from pathlib import Path

from coaching.catalog.loader import TrackCatalogEntry, load_track_yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
MONZA_YAML = (
    REPO_ROOT / "coaching" / "catalog" / "tracks" / "monza.yaml"
)


def test_monza_yaml_parses_to_track_catalog_entry() -> None:
    entry = load_track_yaml(MONZA_YAML)
    assert isinstance(entry, TrackCatalogEntry)
    assert entry.name == "monza"
    assert entry.layout_name == "grand-prix"


def test_monza_length_matches_gp_layout() -> None:
    # Monza GP layout is officially 5793m.
    entry = load_track_yaml(MONZA_YAML)
    assert 5700.0 < entry.length_m < 5900.0


def test_monza_has_two_sector_checkpoints_strictly_increasing() -> None:
    entry = load_track_yaml(MONZA_YAML)
    assert len(entry.sector_checkpoints) == 2
    distances = [cp.distance_m for cp in entry.sector_checkpoints]
    assert distances == sorted(distances)
    assert all(0 < d < entry.length_m for d in distances)


def test_monza_corner_names_include_signature_corners() -> None:
    # Lesmo, Ascari, and Parabolica are canonical Monza landmarks.
    entry = load_track_yaml(MONZA_YAML)
    joined = " ".join(entry.corner_names).lower()
    assert "lesmo" in joined
    assert "ascari" in joined
    assert "parabolica" in joined


def test_monza_start_finish_is_in_lombardy() -> None:
    entry = load_track_yaml(MONZA_YAML)
    # Monza, Italy ~45.62N, 9.28E. Sanity-check ranges.
    assert 45.5 < entry.start_finish_lat < 45.7
    assert 9.2 < entry.start_finish_lon < 9.4
    assert 0.0 <= entry.start_finish_heading_deg < 360.0


def test_monza_master_track_id_differs_from_existing_catalog() -> None:
    monza = load_track_yaml(MONZA_YAML)
    others = [
        load_track_yaml(REPO_ROOT / "coaching" / "catalog" / "tracks" / fn)
        for fn in (
            "sebring.yaml",
            "silverstone.yaml",
            "nurburgring.yaml",
            "red-bull-ring.yaml",
            "laguna-seca.yaml",
            "lime-rock.yaml",
            "ledenon.yaml",
            "charlotte.yaml",
            "spa-francorchamps.yaml",
        )
    ]
    for other in others:
        assert monza.master_track_id != other.master_track_id
