"""T090 (sprint2-backend) — target-aware priority weighting.

Pure-domain function. Returns the priority an ActionPrescription
should carry given:
  - the driver's currently-pinned target_event (None when
    no upcoming pin)
  - the prescription's candidate track_id and car_id (derived
    from the WeaknessEpisode that triggered it)

Two priority levels matches the existing
ActionPrescription.priority schema (String(8); existing scale
is "p0"/"p1" — see infra/orm.py:186 and ADR-005). Match on
track OR car: same-car-different-track still helps (handling
transfer); same-track-different-car still helps (track-knowledge
transfer).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID


def compute_target_aware_priority(
    *,
    target_event: dict[str, Any] | None,
    candidate_track_id: UUID | None,
    candidate_car_id: UUID | None,
) -> str:
    """Return "p0" if the prescription matches the target's
    track or car, "p1" otherwise.

    target_event JSON shape mirrors lap_schemas.v1.TargetEvent —
    track_id and car_id are present-but-may-be-None. None on
    either side means "can't match this axis"; p1 falls through
    naturally without crashes.
    """
    if target_event is None:
        return "p1"
    target_track = target_event.get("track_id")
    target_car = target_event.get("car_id")
    if (
        target_track
        and candidate_track_id is not None
        and str(candidate_track_id) == target_track
    ):
        return "p0"
    if (
        target_car
        and candidate_car_id is not None
        and str(candidate_car_id) == target_car
    ):
        return "p0"
    return "p1"
