"""Sim↔real track equivalence table (T137 / ADR-014).

The same physical circuit captured from iRacing, ACC, AC, or real-
life telemetry must resolve to the same `master_track_id`. This
module loads the admin-curated mapping from
`coaching/catalog/sim_track_equivalence.yaml` and exposes a lookup
API used by the ingest pipeline when stamping `master_track_id`
on a session.
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from coaching.catalog.loader import (
    TrackCatalogParseError,
    get_track_catalog,
)

DEFAULT_EQUIVALENCE_YAML = (
    Path(__file__).resolve().parent / "sim_track_equivalence.yaml"
)


@dataclass(frozen=True)
class SimTrackKey:
    provider: str
    sim_track_id: str


def load_sim_track_equivalence(
    path: Path,
    *,
    validate_against_catalog: bool = False,
) -> dict[SimTrackKey, str]:
    """Read the equivalence YAML into a dict keyed by
    `(provider, sim_track_id)` → `master_track_id`.

    When `validate_against_catalog` is True, every value must
    correspond to a real catalog entry (loaded via
    `get_track_catalog`); a dangling reference raises
    `TrackCatalogParseError`.
    """
    with path.open(encoding="utf-8") as f:
        data: Any = yaml.safe_load(f)

    if not isinstance(data, dict) or "pairs" not in data:
        raise TrackCatalogParseError(
            "expected_top_level_dict_with_pairs"
        )

    table: dict[SimTrackKey, str] = {}
    for entry in data["pairs"]:
        key = SimTrackKey(
            provider=str(entry["provider"]),
            sim_track_id=str(entry["sim_track_id"]),
        )
        master_id = str(entry["master_track_id"])
        if key in table:
            raise TrackCatalogParseError(
                f"duplicate_sim_track_key: {key}"
            )
        table[key] = master_id

    if validate_against_catalog:
        catalog_ids = set(get_track_catalog().keys())
        for key, master_id in table.items():
            if master_id not in catalog_ids:
                raise TrackCatalogParseError(
                    f"unknown_master_track_id: {master_id} "
                    f"referenced by {key}"
                )

    return table


@lru_cache(maxsize=1)
def get_sim_track_equivalence() -> dict[SimTrackKey, str]:
    """Production accessor: lazy-loaded, cached. Validates against
    the catalog on first call so a dangling reference fails fast."""
    return load_sim_track_equivalence(
        DEFAULT_EQUIVALENCE_YAML,
        validate_against_catalog=True,
    )


def lookup_master_track_id(
    *,
    provider: str,
    sim_track_id: str,
) -> str | None:
    """Resolve a sim-specific identifier to a `master_track_id`,
    or `None` if no mapping is registered."""
    return get_sim_track_equivalence().get(
        SimTrackKey(provider=provider, sim_track_id=sim_track_id)
    )
