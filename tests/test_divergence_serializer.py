"""T041 (sprint2-backend) — divergence point serialization.

Convert a corner-level CornerDivergence (T039/T040 output) into
per-metric DivergencePoint records that match the persistence
schema (lap-schemas v1). Each metric (e.g. "brake_pressure_peak")
becomes its own DivergencePoint; they share the corner's track
position and the corner-level divergence_type.

Why per-metric:
  The schema stores DivergencePoints in transfer_links.divergence_points
  for charting. Each (metric, sim, real) row drives one chart trace.

The serializer is a pure function — no DB writes here. T041
"persistence" = produces validated, DB-shaped records; the bridge
endpoint pipeline that already exists writes them.
"""

from __future__ import annotations

import pytest
from lap_schemas.v1.divergence_point import DivergencePoint
from pydantic import ValidationError

from domain.bridge_divergence import CornerDivergence
from domain.divergence_serializer import (
    ChannelEvidence,
    serialize_divergence_points,
)


def _cd(divtype: str = "intensity") -> CornerDivergence:
    return CornerDivergence(
        corner_name="T1",
        divergence_type=divtype,  # type: ignore[arg-type]
        shape_score=0.95,
        intensity_score=0.4,
    )


def _ev(
    metric: str = "brake_pressure_peak",
    *,
    sim: float = 1.0,
    real: float = 0.6,
) -> ChannelEvidence:
    return ChannelEvidence(metric=metric, sim_value=sim, real_value=real)


# -- shape --------------------------------------------------------


def test_one_divergence_point_per_evidence_entry() -> None:
    """Three pieces of channel evidence → three DivergencePoints,
    in input order. Caller can rely on stable ordering."""
    out = serialize_divergence_points(
        _cd(),
        track_distance_m=120.0,
        evidence=[
            _ev("brake_pressure_peak", sim=1.0, real=0.6),
            _ev("brake_pressure_integral", sim=50.0, real=30.0),
            _ev("speed_min", sim=120.0, real=110.0),
        ],
    )
    assert len(out) == 3
    assert [p.metric for p in out] == [
        "brake_pressure_peak",
        "brake_pressure_integral",
        "speed_min",
    ]


def test_returns_pydantic_divergence_points() -> None:
    """Output is the lap-schemas Pydantic model — not a dict, not
    our internal dataclass. Persistence rides on this contract."""
    out = serialize_divergence_points(
        _cd(),
        track_distance_m=120.0,
        evidence=[_ev()],
    )
    assert isinstance(out[0], DivergencePoint)


def test_empty_evidence_returns_empty_list() -> None:
    """A divergence detected at corner level but with no per-metric
    evidence emits zero rows — consumer-safe, never crashes."""
    out = serialize_divergence_points(
        _cd(), track_distance_m=120.0, evidence=[]
    )
    assert out == []


# -- field propagation --------------------------------------------


def test_track_distance_m_propagated_to_every_point() -> None:
    out = serialize_divergence_points(
        _cd(),
        track_distance_m=320.5,
        evidence=[_ev("brake"), _ev("speed")],
    )
    assert {p.track_distance_m for p in out} == {320.5}


def test_divergence_type_propagated_from_corner() -> None:
    """The corner-level type carries to each metric — the schema's
    Literal stays consistent across the per-metric expansion."""
    out_intensity = serialize_divergence_points(
        _cd("intensity"), track_distance_m=120.0, evidence=[_ev()]
    )
    out_shape = serialize_divergence_points(
        _cd("shape"), track_distance_m=120.0, evidence=[_ev()]
    )
    assert out_intensity[0].divergence_type == "intensity"
    assert out_shape[0].divergence_type == "shape"


def test_sim_and_real_values_propagated_per_metric() -> None:
    """Each evidence entry's sim/real values land on its own row —
    no leakage between metrics."""
    out = serialize_divergence_points(
        _cd(),
        track_distance_m=120.0,
        evidence=[
            _ev("brake", sim=1.0, real=0.6),
            _ev("speed", sim=120.0, real=110.0),
        ],
    )
    brake = next(p for p in out if p.metric == "brake")
    speed = next(p for p in out if p.metric == "speed")
    assert (brake.sim_value, brake.real_value) == (1.0, 0.6)
    assert (speed.sim_value, speed.real_value) == (120.0, 110.0)


# -- schema validation --------------------------------------------


def test_negative_track_distance_raises_validation() -> None:
    """The schema's `track_distance_m: Field(ge=0)` is enforced —
    serialization fails loud rather than persisting bad data."""
    with pytest.raises(ValidationError):
        serialize_divergence_points(
            _cd(),
            track_distance_m=-1.0,
            evidence=[_ev()],
        )


def test_empty_metric_raises_validation() -> None:
    """The schema's `metric: Field(min_length=1)` is enforced —
    a blank metric label can't be charted, so we reject."""
    with pytest.raises(ValidationError):
        serialize_divergence_points(
            _cd(),
            track_distance_m=120.0,
            evidence=[_ev("")],
        )


def test_serialized_points_are_frozen() -> None:
    """The schema's `frozen=True` config — once produced, can't be
    mutated. Catches a regression where someone removes frozen and
    consumers start mutating shared state."""
    out = serialize_divergence_points(
        _cd(), track_distance_m=120.0, evidence=[_ev()]
    )
    with pytest.raises(ValidationError):
        out[0].sim_value = 99.0  # type: ignore[misc]
