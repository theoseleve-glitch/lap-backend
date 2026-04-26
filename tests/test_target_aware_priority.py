"""T090 (sprint2-backend) — target-aware prescription priority.

When a driver has pinned a TargetEvent (T082) and the readiness
recompute hook (T089) carries that event into
DriverReadinessState.target_event, prescription priority must
weight upward when the candidate prescription matches the
upcoming event's track or car.

Pure-domain function. The caller (T211 prescription generator)
hands in:
  - target_event JSON (None when driver has no pin)
  - candidate_track_id / candidate_car_id (the prescription's
    relevant context — derived from the WeaknessEpisode's most
    recent session)

Returns "p0" when there's a match, "p1" otherwise. Two
priority levels matches the existing ActionPrescription.priority
shape (String(8) — see infra/orm.py:186); a richer scale would
require schema-side changes out of scope for T090.

Match semantics: track_id OR car_id is sufficient. Practising
the same car at a different track still helps prep (car-handling
transfer); practising the target track in any car still helps
prep (track-knowledge transfer). Requiring both would shrink the
boost to near-zero in practice — a driver rarely re-runs the
exact same sim+car combo a week before the event.

Tests:
  - no target → always p1 (sprint-only driver baseline)
  - target with null track/car → always p1 (e.g., a track day
    venue not in the catalog yet)
  - track match → p0
  - car match → p0
  - both match → p0 (idempotent — not p-2 or some bogus value)
  - neither match → p1
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from domain.target_match import compute_target_aware_priority


def _target(
    *,
    track_id: UUID | None = None,
    car_id: UUID | None = None,
) -> dict[str, Any]:
    """Synthetic target_event JSON matching the shape recompute_
    driver_readiness writes (str-cast UUIDs for JSON portability)."""
    return {
        "id": str(uuid4()),
        "name": "synthetic target",
        "kind": "sim_event",
        "starts_at": "2026-05-01T00:00:00+00:00",
        "ends_at": None,
        "track_id": str(track_id) if track_id else None,
        "car_id": str(car_id) if car_id else None,
        "competitive_weight": 0.7,
        "notes": None,
    }


# -- baseline ----------------------------------------------------


def test_no_target_event_returns_p1() -> None:
    """Driver has no pin → no signal to bias priority. Pin: a
    sprint-only driver's prescription queue must NOT spuriously
    inherit p0 just because the function ran with target=None."""
    assert compute_target_aware_priority(
        target_event=None,
        candidate_track_id=uuid4(),
        candidate_car_id=uuid4(),
    ) == "p1"


def test_target_with_null_track_and_car_returns_p1() -> None:
    """Real-life track day pinned with no catalog mapping yet
    (T088 venue isn't in our track catalog). target_event JSON
    has track_id=None and car_id=None. No way to match → p1.
    Pin: a regression that boosts on "any pin" would make every
    prescription p0 once the user pins anything."""
    assert compute_target_aware_priority(
        target_event=_target(track_id=None, car_id=None),
        candidate_track_id=uuid4(),
        candidate_car_id=uuid4(),
    ) == "p1"


# -- match paths -------------------------------------------------


def test_track_match_returns_p0() -> None:
    """Prescription on the same track as the target → boost.
    Same-track practice is direct event prep; the driver should
    see this prescription before unrelated ones."""
    track = uuid4()
    assert compute_target_aware_priority(
        target_event=_target(track_id=track, car_id=uuid4()),
        candidate_track_id=track,
        candidate_car_id=uuid4(),
    ) == "p0"


def test_car_match_returns_p0() -> None:
    """Prescription in the same car as the target → boost.
    Car-handling transfers across tracks; a brake-modulation
    drill in the target car prepares the driver even at a
    different venue. Pin: dropping this OR clause would halve
    the boost surface area."""
    car = uuid4()
    assert compute_target_aware_priority(
        target_event=_target(track_id=uuid4(), car_id=car),
        candidate_track_id=uuid4(),
        candidate_car_id=car,
    ) == "p0"


def test_both_match_returns_p0_not_some_higher_grade() -> None:
    """Track AND car both match → still p0. ActionPrescription.
    priority is String(8) and the existing scale is p0/p1; this
    function must not invent a "p-1" or "p00". Pin: a regression
    to a numeric scale would break the schema's char limit."""
    track = uuid4()
    car = uuid4()
    assert compute_target_aware_priority(
        target_event=_target(track_id=track, car_id=car),
        candidate_track_id=track,
        candidate_car_id=car,
    ) == "p0"


# -- no match ---------------------------------------------------


def test_neither_track_nor_car_match_returns_p1() -> None:
    """Different track AND different car → no boost. The
    prescription is unrelated to the upcoming event's prep;
    leave at the default. Pin: the inverse of the match cases —
    a regression that ALWAYS returned p0 when target!=None
    would also satisfy the match cases above."""
    assert compute_target_aware_priority(
        target_event=_target(track_id=uuid4(), car_id=uuid4()),
        candidate_track_id=uuid4(),
        candidate_car_id=uuid4(),
    ) == "p1"


# -- candidate-side null robustness ------------------------------


def test_candidate_with_null_track_and_car_returns_p1() -> None:
    """Episode-side might have no track/car context (early-
    sprint episode before catalog resolution lands). Pin: must
    not crash with TypeError when comparing None to str."""
    assert compute_target_aware_priority(
        target_event=_target(track_id=uuid4(), car_id=uuid4()),
        candidate_track_id=None,
        candidate_car_id=None,
    ) == "p1"
