"""T134 — Spa-Francorchamps YAML in the track catalog.

Adds Circuit de Spa-Francorchamps (Stavelot, Belgium — F1 / WEC
venue). Asserts the loader returns reasonable values for length,
sector boundaries, signature corner names (Eau Rouge / Raidillon,
Pouhon, Blanchimont), and start/finish coordinates inside the
Belgian Ardennes.
"""

from pathlib import Path

from coaching.catalog.loader import TrackCatalogEntry, load_track_yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SPA_YAML = (
    REPO_ROOT / "coaching" / "catalog" / "tracks" / "spa-francorchamps.yaml"
)


def test_spa_yaml_parses_to_track_catalog_entry() -> None:
    entry = load_track_yaml(SPA_YAML)
    assert isinstance(entry, TrackCatalogEntry)
    assert entry.name == "spa-francorchamps"
    assert entry.layout_name == "grand-prix"


def test_spa_length_matches_gp_layout() -> None:
    # Spa GP layout is officially 7004m post-2022 reprofiling.
    entry = load_track_yaml(SPA_YAML)
    assert 6900.0 < entry.length_m < 7100.0


def test_spa_has_two_sector_checkpoints_strictly_increasing() -> None:
    entry = load_track_yaml(SPA_YAML)
    assert len(entry.sector_checkpoints) == 2
    distances = [cp.distance_m for cp in entry.sector_checkpoints]
    assert distances == sorted(distances)
    assert all(0 < d < entry.length_m for d in distances)


def test_spa_corner_names_include_signature_corners() -> None:
    # Eau Rouge / Raidillon, Pouhon, and Blanchimont are the
    # canonical Spa landmarks.
    entry = load_track_yaml(SPA_YAML)
    joined = " ".join(entry.corner_names).lower()
    assert "eau rouge" in joined or "raidillon" in joined
    assert "pouhon" in joined
    assert "blanchimont" in joined


def test_spa_start_finish_is_in_the_belgian_ardennes() -> None:
    entry = load_track_yaml(SPA_YAML)
    # Stavelot, Belgium ~50.44N, 5.97E. Sanity-check ranges.
    assert 50.3 < entry.start_finish_lat < 50.55
    assert 5.85 < entry.start_finish_lon < 6.10
    assert 0.0 <= entry.start_finish_heading_deg < 360.0


def test_spa_master_track_id_differs_from_existing_catalog() -> None:
    spa = load_track_yaml(SPA_YAML)
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
        )
    ]
    for other in others:
        assert spa.master_track_id != other.master_track_id
