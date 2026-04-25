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
from pathlib import Path
from typing import Any

import yaml


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
