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
