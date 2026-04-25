"""Track / car catalog read endpoints (T140+ / ADR-014).

Unauthenticated: catalog is non-sensitive reference data clients
need before login (e.g., to populate "select your track" UI in
the registration flow). Pagination by limit/offset against the
in-memory catalog snapshot loaded at startup by `get_track_catalog`.
"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from lap_schemas.v1 import (
    SectorCheckpointPublic,
    TrackCatalogList,
    TrackCatalogPublic,
)

from coaching.catalog.loader import TrackCatalogEntry, get_track_catalog

router = APIRouter(prefix="/api/v1/catalog", tags=["catalog"])

MAX_LIMIT = 200


def _to_public(entry: TrackCatalogEntry) -> TrackCatalogPublic:
    return TrackCatalogPublic(
        master_track_id=entry.master_track_id,
        name=entry.name,
        layout_name=entry.layout_name,
        length_m=entry.length_m,
        start_finish_lat=entry.start_finish_lat,
        start_finish_lon=entry.start_finish_lon,
        start_finish_heading_deg=entry.start_finish_heading_deg,
        sector_checkpoints=tuple(
            SectorCheckpointPublic(distance_m=cp.distance_m, name=cp.name)
            for cp in entry.sector_checkpoints
        ),
        corner_names=entry.corner_names,
    )


@router.get("/tracks", response_model=TrackCatalogList)
async def list_tracks(
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TrackCatalogList:
    catalog = get_track_catalog()
    # Stable ordering: sort by master_track_id so pagination is
    # deterministic across calls (the catalog dict is insertion-
    # ordered per filename glob, but pinning the sort key removes
    # any reliance on filesystem traversal order).
    ordered = sorted(catalog.values(), key=lambda e: e.master_track_id)
    page = ordered[offset : offset + limit]
    return TrackCatalogList(
        items=tuple(_to_public(e) for e in page),
        total=len(ordered),
        limit=limit,
        offset=offset,
    )


@router.get(
    "/tracks/{master_track_id}",
    response_model=TrackCatalogPublic,
)
async def get_track(master_track_id: str) -> TrackCatalogPublic:
    entry = get_track_catalog().get(master_track_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="track_not_found",
        )
    return _to_public(entry)
