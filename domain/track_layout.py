"""Track layout value object (T117, T118).

Carries the static geometry of a circuit: name, total length, and
the sector boundary distances (T118). Boundaries are metres-from-
lap-start, strictly increasing; two boundaries → three sectors.
Default `()` keeps the field optional for callers that only need
length-aware lap detection.

Kept as a plain frozen dataclass so domain code can use it as a
cache key without importing ORM machinery.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TrackLayout:
    name: str
    length_m: float
    sector_boundaries_m: tuple[float, ...] = field(default_factory=tuple)
