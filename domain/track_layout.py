"""Track layout value object (T117).

Carries the static geometry of a circuit — currently just name and
length, but T118 will extend it with sector checkpoints. Kept as a
plain frozen dataclass so domain code can use it as a cache key
without importing ORM machinery.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TrackLayout:
    name: str
    length_m: float
