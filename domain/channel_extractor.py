"""T032 (sprint2-backend) — channel extraction.

Walk a telemetry sample stream and pull out the six standard
channels bridge analysis works on: brake, throttle, steering,
speed, g_lat, g_lon. Output is a column-oriented ChannelFrame
("DataFrame-shaped" — named columns, equal-length values) without
a literal pandas dependency. T033 will introduce numpy when the
cubic-spline resample needs it.

Missing or non-numeric values are filled with NaN (not None) so
downstream cross-correlation (T034) can detect gaps via
`math.isnan` without an extra null check in every inner loop.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

CHANNELS = ("brake", "throttle", "steering", "speed", "g_lat", "g_lon")


@dataclass(frozen=True)
class ChannelFrame:
    brake: tuple[float, ...]
    throttle: tuple[float, ...]
    steering: tuple[float, ...]
    speed: tuple[float, ...]
    g_lat: tuple[float, ...]
    g_lon: tuple[float, ...]

    def __post_init__(self) -> None:
        sizes = {
            len(self.brake), len(self.throttle), len(self.steering),
            len(self.speed), len(self.g_lat), len(self.g_lon),
        }
        if len(sizes) > 1:
            raise ValueError(
                f"channel lengths must be equal, got: {sorted(sizes)}"
            )

    def __len__(self) -> int:
        return len(self.brake)


def _as_float(v: object) -> float:
    """Coerce v to float; return NaN for missing / non-numeric.
    Booleans are excluded so True/False don't sneak through as
    1.0/0.0 — telemetry values are real numbers, not flags."""
    if isinstance(v, bool):
        return math.nan
    if isinstance(v, int | float):
        return float(v)
    return math.nan


def extract_channels(
    samples: Iterable[dict[str, object]],
) -> ChannelFrame:
    cols: dict[str, list[float]] = {name: [] for name in CHANNELS}
    for sample in samples:
        for name in CHANNELS:
            cols[name].append(_as_float(sample.get(name)))
    return ChannelFrame(
        brake=tuple(cols["brake"]),
        throttle=tuple(cols["throttle"]),
        steering=tuple(cols["steering"]),
        speed=tuple(cols["speed"]),
        g_lat=tuple(cols["g_lat"]),
        g_lon=tuple(cols["g_lon"]),
    )
