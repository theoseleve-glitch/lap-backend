"""T184 — feature namespace for coaching rule conditions.

`FEATURE_NAMESPACE` is the closed set of names a rule's condition
expression may reference. The compiler (T183) verifies syntax
safety; this module catches the second class of error — name
typos. A rule that references `apex_speeed` fails at load time
with `UnknownFeaturesError`, not silently at runtime.

`FEATURES` carries the per-name metadata (unit, description, kind)
that downstream tooling — the runner, the docs site, the future
rule-authoring UI — relies on. Each feature is either:
  - PIPELINE  — derived from the current session's telemetry
  - REFERENCE — looked up from a benchmark (driver PB, cohort,
                track-level reference). The runner (T185) reads
                this kind to know where to fetch the value.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class FeatureKind(StrEnum):
    PIPELINE = "pipeline"
    REFERENCE = "reference"


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    unit: str
    description: str
    kind: FeatureKind


# Authoring note: when the pipeline gains a new metric (e.g.,
# brake_peak), add it here so rules can reference it. The runner
# fails fast if a rule references a name that's not registered.
_FEATURE_LIST: tuple[FeatureSpec, ...] = (
    # --- existing pipeline features (T119 SessionMetrics) ---
    FeatureSpec(
        name="best_lap_ms",
        unit="milliseconds",
        description="Fastest valid lap in the session.",
        kind=FeatureKind.PIPELINE,
    ),
    FeatureSpec(
        name="avg_lap_ms",
        unit="milliseconds",
        description="Mean of valid lap times in the session.",
        kind=FeatureKind.PIPELINE,
    ),
    FeatureSpec(
        name="consistency_ms",
        unit="milliseconds",
        description="Stdev of valid lap times — lower is more consistent.",
        kind=FeatureKind.PIPELINE,
    ),
    FeatureSpec(
        name="peak_speed_kph",
        unit="km/h",
        description="Highest speed reading anywhere in the session.",
        kind=FeatureKind.PIPELINE,
    ),
    # --- features used in ADR-013 example ---
    # Future pipeline work (post-T185) will fill these from
    # corner-segmented telemetry. The names enter the namespace
    # now so authoring rules ahead of time doesn't trip the typo
    # check.
    FeatureSpec(
        name="apex_speed",
        unit="km/h",
        description="Minimum speed reached at a corner apex.",
        kind=FeatureKind.PIPELINE,
    ),
    FeatureSpec(
        name="brake_peak",
        unit="g",
        description="Peak longitudinal deceleration during braking.",
        kind=FeatureKind.PIPELINE,
    ),
    FeatureSpec(
        name="reference_apex_speed",
        unit="km/h",
        description=(
            "Benchmark apex speed (driver PB / cohort median) "
            "to compare against."
        ),
        kind=FeatureKind.REFERENCE,
    ),
    # T189 — early-braking detection.
    FeatureSpec(
        name="entry_speed",
        unit="km/h",
        description=(
            "Speed at the corner entry waypoint (start of the "
            "primary braking zone)."
        ),
        kind=FeatureKind.PIPELINE,
    ),
    FeatureSpec(
        name="reference_entry_speed",
        unit="km/h",
        description=(
            "Benchmark entry speed (driver PB / cohort median) "
            "to compare against."
        ),
        kind=FeatureKind.REFERENCE,
    ),
    # T190 — brake-release rate (derivative signal; first
    # non-snapshot feature in the namespace).
    FeatureSpec(
        name="brake_release_rate",
        unit="g/s",
        description=(
            "Rate at which longitudinal deceleration drops as "
            "the brake pedal is released. High values = abrupt "
            "release, low values = smooth trail-brake."
        ),
        kind=FeatureKind.PIPELINE,
    ),
    FeatureSpec(
        name="reference_brake_release_rate",
        unit="g/s",
        description=(
            "Benchmark brake-release rate for the corner."
        ),
        kind=FeatureKind.REFERENCE,
    ),
    # T191 — lap-to-lap repeatability metric. No reference
    # benchmark needed; consistency is an absolute property.
    FeatureSpec(
        name="brake_pressure_cov_in_sector",
        unit="ratio",
        description=(
            "Coefficient of variation (stdev/mean) of peak "
            "brake pressure across consecutive laps in a "
            "sector. Higher = more inconsistent."
        ),
        kind=FeatureKind.PIPELINE,
    ),
    # T192 — discrete-event count, no reference benchmark.
    FeatureSpec(
        name="lockup_event_count",
        unit="count",
        description=(
            "Number of lockup events detected in the session "
            "(wheel speed dropping well below vehicle speed)."
        ),
        kind=FeatureKind.PIPELINE,
    ),
    # T193 — cold-brake deficit. Driver's lap-1 brake distance
    # divided by their warm-brake reference (later laps in the
    # same session, or PB).
    FeatureSpec(
        name="first_lap_brake_distance_ratio",
        unit="ratio",
        description=(
            "Lap-1 average brake distance divided by the "
            "driver's warm-brake reference. >1.0 means longer "
            "than warm; cold brakes typically read 1.10-1.20."
        ),
        kind=FeatureKind.PIPELINE,
    ),
    # T194 — residual brake at apex. Driver who's still on the
    # brake at apex isn't trail-braking off, they're scrubbing.
    FeatureSpec(
        name="mid_corner_brake_pressure_pct",
        unit="ratio",
        description=(
            "Apex-region brake pressure as a fraction of peak "
            "entry brake pressure. Should be near 0 at apex; "
            "values >0.20 mean residual brake is dragging speed."
        ),
        kind=FeatureKind.PIPELINE,
    ),
    # T195 — front/rear balance during entry braking. Direction-
    # agnostic: high values = rear locking before front
    # regardless of absolute slip magnitude.
    FeatureSpec(
        name="rear_front_slip_ratio_entry",
        unit="ratio",
        description=(
            "Rear-axle slip divided by front-axle slip during "
            "the entry-braking phase. >1.5 means rear is "
            "sliding much more than front (bias too rear, or "
            "front insufficiently loaded)."
        ),
        kind=FeatureKind.PIPELINE,
    ),
    # T196 — where in the corner-entry distance the brake comes
    # fully off. 0% = at the braking marker, 100% = at the apex.
    FeatureSpec(
        name="brake_release_position_pct",
        unit="ratio",
        description=(
            "Position along the braking-marker → apex distance "
            "where the brake reaches zero pressure. 0.0 = at "
            "the marker, 1.0 = at the apex. Target window is "
            "0.80-1.00 for a proper trail-brake handoff."
        ),
        kind=FeatureKind.PIPELINE,
    ),
    # T197 — peak braking deceleration as a fraction of the
    # tires' theoretical grip ceiling. <0.85 = under-driven.
    FeatureSpec(
        name="peak_decel_grip_ratio",
        unit="ratio",
        description=(
            "Peak braking deceleration as a fraction of the "
            "tires' theoretical max grip (longitudinal μ × g). "
            "Pros sit ~0.95; values <0.85 mean braking distance "
            "is being left on the table."
        ),
        kind=FeatureKind.PIPELINE,
    ),
)

FEATURES: dict[str, FeatureSpec] = {f.name: f for f in _FEATURE_LIST}
FEATURE_NAMESPACE: frozenset[str] = frozenset(FEATURES.keys())


class UnknownFeaturesError(Exception):
    """Raised when a rule references a feature outside the namespace."""


def require_known_features(names: Iterable[str]) -> None:
    """Reject any name that isn't in `FEATURE_NAMESPACE`. Lists
    all unknowns at once so an operator fixes the YAML in one
    pass instead of one-typo-per-startup-attempt."""
    unknown = sorted(set(names) - FEATURE_NAMESPACE)
    if unknown:
        raise UnknownFeaturesError(
            f"unknown features: {unknown}. "
            f"Add to coaching/library/features.py or fix the typo."
        )
