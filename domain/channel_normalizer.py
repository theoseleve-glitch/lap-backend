"""T033 (sprint2-backend) — channel normalization.

Cubic-spline resample of a ChannelFrame onto an evenly-spaced
target_size-point grid. Sprint mandates 500 points per corner —
the size at which T034 cross-correlation compares sessions with
different sample rates apples-to-apples.

Stability properties:
- output frame always has exactly target_size rows
- a column with any NaN in the input becomes all-NaN in the
  output (we don't fabricate data across gaps)
- duplicate x values are deduplicated (last wins)
- < 4 input rows raises ValueError (CubicSpline minimum)
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from scipy.interpolate import CubicSpline

from domain.channel_extractor import CHANNELS, ChannelFrame

DEFAULT_TARGET_SIZE = 500
_MIN_POINTS = 4


def _dedupe_x(
    x: np.ndarray, ys: dict[str, np.ndarray]
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Keep the last sample at each unique x. Real-world capture
    sometimes duplicates (high rate × stationary car). scipy's
    CubicSpline rejects non-strictly-increasing x, so we collapse
    here rather than reject upstream."""
    # np.unique with return_index gives sorted unique x and the
    # index of *first* occurrence. We want last — so reverse,
    # uniqify, reverse back.
    _, last_idx_in_reversed = np.unique(x[::-1], return_index=True)
    keep_idx = sorted(len(x) - 1 - last_idx_in_reversed)
    x_out = x[keep_idx]
    ys_out = {name: arr[keep_idx] for name, arr in ys.items()}
    return x_out, ys_out


def normalize_channels(
    frame: ChannelFrame,
    x_values: Sequence[float],
    *,
    target_size: int = DEFAULT_TARGET_SIZE,
) -> ChannelFrame:
    if len(x_values) != len(frame):
        raise ValueError(
            f"x_values length {len(x_values)} != frame length "
            f"{len(frame)}"
        )
    if len(frame) < _MIN_POINTS:
        raise ValueError(
            f"normalize requires >= {_MIN_POINTS} points, "
            f"got {len(frame)}"
        )

    x = np.asarray(x_values, dtype=float)
    ys = {name: np.asarray(getattr(frame, name), dtype=float) for name in CHANNELS}
    x, ys = _dedupe_x(x, ys)

    if len(x) < _MIN_POINTS:
        raise ValueError(
            f"normalize requires >= {_MIN_POINTS} unique x values, "
            f"got {len(x)} after dedup"
        )

    target_x = np.linspace(x[0], x[-1], target_size)

    out: dict[str, tuple[float, ...]] = {}
    for name, y in ys.items():
        if np.isnan(y).any():
            # NaN poisoning: don't fabricate across gaps.
            out[name] = tuple([math.nan] * target_size)
            continue
        spline = CubicSpline(x, y)
        out[name] = tuple(float(v) for v in spline(target_x))

    return ChannelFrame(
        brake=out["brake"],
        throttle=out["throttle"],
        steering=out["steering"],
        speed=out["speed"],
        g_lat=out["g_lat"],
        g_lon=out["g_lon"],
    )
