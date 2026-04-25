"""Track catalog YAML loader (T126).

Each circuit lives as a YAML file under
`coaching/catalog/tracks/<name>.yaml`. This module reads one file
and returns a frozen `TrackCatalogEntry` dataclass.

Schema (matches ADR-014 + the `track_layouts` ORM columns):
    source_provider: str
    name: str
    layout_name: str
    length_m: float
    master_track_id: str  (UUID7 string; sim↔real equivalence anchor)
    start_finish: { lat: float, lon: float, heading_deg: float }
    sector_checkpoints: [{ distance_m: float, name: str }, ...]
    corner_names: [str, ...]

The model is a dataclass rather than a Pydantic model because
catalog entries are server-internal infrastructure, not part of
the wire schema (lap-schemas) — keeping it here avoids cross-repo
coordination for what is effectively config.
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

DEFAULT_TRACKS_DIR = Path(__file__).resolve().parent / "tracks"


class TrackCatalogParseError(ValueError):
    """A track YAML failed to parse or validate."""


@dataclass(frozen=True)
class SectorCheckpoint:
    distance_m: float
    name: str


@dataclass(frozen=True)
class TrackCatalogEntry:
    source_provider: str
    name: str
    layout_name: str
    length_m: float
    master_track_id: str
    start_finish_lat: float
    start_finish_lon: float
    start_finish_heading_deg: float
    sector_checkpoints: tuple[SectorCheckpoint, ...]
    corner_names: tuple[str, ...]


_REQUIRED_TOP_LEVEL = (
    "source_provider",
    "name",
    "layout_name",
    "length_m",
    "master_track_id",
    "start_finish",
    "sector_checkpoints",
    "corner_names",
)
_REQUIRED_START_FINISH = ("lat", "lon", "heading_deg")


def load_track_yaml(path: Path) -> TrackCatalogEntry:
    try:
        with path.open(encoding="utf-8") as f:
            data: Any = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise TrackCatalogParseError(f"yaml_parse_error: {exc}") from exc

    if not isinstance(data, dict):
        raise TrackCatalogParseError(
            "expected_top_level_dict, got non-dict"
        )

    for field in _REQUIRED_TOP_LEVEL:
        if field not in data:
            raise TrackCatalogParseError(f"missing_field: {field}")

    sf = data["start_finish"]
    if not isinstance(sf, dict) or any(
        k not in sf for k in _REQUIRED_START_FINISH
    ):
        raise TrackCatalogParseError(
            "incomplete_start_finish: needs lat/lon/heading_deg"
        )

    checkpoints = tuple(
        SectorCheckpoint(
            distance_m=float(cp["distance_m"]),
            name=str(cp["name"]),
        )
        for cp in data["sector_checkpoints"]
    )

    return TrackCatalogEntry(
        source_provider=str(data["source_provider"]),
        name=str(data["name"]),
        layout_name=str(data["layout_name"]),
        length_m=float(data["length_m"]),
        master_track_id=str(data["master_track_id"]),
        start_finish_lat=float(sf["lat"]),
        start_finish_lon=float(sf["lon"]),
        start_finish_heading_deg=float(sf["heading_deg"]),
        sector_checkpoints=checkpoints,
        corner_names=tuple(str(n) for n in data["corner_names"]),
    )


def load_track_catalog(tracks_dir: Path) -> dict[str, TrackCatalogEntry]:
    """Read every `*.yaml` under `tracks_dir` and key them by
    master_track_id. Raises `TrackCatalogParseError` on duplicate
    IDs (sim↔real equivalence requires uniqueness)."""
    catalog: dict[str, TrackCatalogEntry] = {}
    for path in sorted(tracks_dir.glob("*.yaml")):
        entry = load_track_yaml(path)
        if entry.master_track_id in catalog:
            existing = catalog[entry.master_track_id]
            raise TrackCatalogParseError(
                "duplicate_master_track_id: "
                f"{entry.master_track_id} used by both "
                f"{existing.name!r} and {entry.name!r}"
            )
        catalog[entry.master_track_id] = entry
    return catalog


@lru_cache(maxsize=1)
def get_track_catalog() -> dict[str, TrackCatalogEntry]:
    """Production accessor: lazy-loaded, cached for the process
    lifetime. First call walks `DEFAULT_TRACKS_DIR`; subsequent
    calls return the same dict object."""
    return load_track_catalog(DEFAULT_TRACKS_DIR)


# --- Car catalog (T138 / ADR-014) ----------------------------------

DEFAULT_CARS_DIR = Path(__file__).resolve().parent / "cars"


@dataclass(frozen=True)
class CarCatalogEntry:
    source_provider: str
    name: str
    manufacturer: str
    model: str
    year: int
    car_class: str  # "GT3", "Karting/KZ", "Karting/X30", ...
    master_car_id: str  # UUID7 string; sim↔real equivalence anchor
    mass_kg: float  # race trim incl. driver
    power_kw: float  # peak crank power
    drive_type: str  # "RWD" | "FWD" | "AWD"
    aspiration: str  # "NA" | "Turbo" | "Supercharged" | "Hybrid"


_REQUIRED_CAR_FIELDS = (
    "source_provider",
    "name",
    "manufacturer",
    "model",
    "year",
    "car_class",
    "master_car_id",
    "mass_kg",
    "power_kw",
    "drive_type",
    "aspiration",
)


def load_car_yaml(path: Path) -> CarCatalogEntry:
    try:
        with path.open(encoding="utf-8") as f:
            data: Any = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise TrackCatalogParseError(f"yaml_parse_error: {exc}") from exc

    if not isinstance(data, dict):
        raise TrackCatalogParseError(
            "expected_top_level_dict, got non-dict"
        )

    for field in _REQUIRED_CAR_FIELDS:
        if field not in data:
            raise TrackCatalogParseError(f"missing_field: {field}")

    return CarCatalogEntry(
        source_provider=str(data["source_provider"]),
        name=str(data["name"]),
        manufacturer=str(data["manufacturer"]),
        model=str(data["model"]),
        year=int(data["year"]),
        car_class=str(data["car_class"]),
        master_car_id=str(data["master_car_id"]),
        mass_kg=float(data["mass_kg"]),
        power_kw=float(data["power_kw"]),
        drive_type=str(data["drive_type"]),
        aspiration=str(data["aspiration"]),
    )


def load_car_catalog(cars_dir: Path) -> dict[str, CarCatalogEntry]:
    """Read every `*.yaml` under `cars_dir` and key them by
    master_car_id. Raises `TrackCatalogParseError` on duplicate IDs."""
    catalog: dict[str, CarCatalogEntry] = {}
    for path in sorted(cars_dir.glob("*.yaml")):
        entry = load_car_yaml(path)
        if entry.master_car_id in catalog:
            existing = catalog[entry.master_car_id]
            raise TrackCatalogParseError(
                "duplicate_master_car_id: "
                f"{entry.master_car_id} used by both "
                f"{existing.name!r} and {entry.name!r}"
            )
        catalog[entry.master_car_id] = entry
    return catalog


@lru_cache(maxsize=1)
def get_car_catalog() -> dict[str, CarCatalogEntry]:
    """Production accessor: lazy-loaded, cached for the process
    lifetime. First call walks `DEFAULT_CARS_DIR`."""
    return load_car_catalog(DEFAULT_CARS_DIR)
