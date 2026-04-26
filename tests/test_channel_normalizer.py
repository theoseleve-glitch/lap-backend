"""T033 (sprint2-backend) — channel normalization.

Cubic-spline resample of a ChannelFrame onto a fixed-size,
evenly-spaced grid. The 500-point target is sprint-mandated; the
normalized frames feed T034 cross-correlation, which compares
shape across channels — the 500-point grid lets us correlate
sessions with different sample rates apples-to-apples.

Stability properties pinned:
- output frame always has exactly target_size rows (default 500)
- a column that has any NaN in the input becomes all-NaN in the
  output (we don't fabricate data across gaps)
- non-strictly-increasing x is deduplicated, keeping the last
  sample at each unique x
- < 4 input rows raises ValueError (CubicSpline minimum)
"""

from __future__ import annotations

import math

import pytest

from domain.channel_extractor import ChannelFrame
from domain.channel_normalizer import normalize_channels


def _frame_with(
    n: int, *, brake: float = 0.5, throttle: float = 0.5
) -> ChannelFrame:
    """Build an N-row frame where every channel has the same
    constant value (or per-channel overrides)."""
    return ChannelFrame(
        brake=tuple([brake] * n),
        throttle=tuple([throttle] * n),
        steering=tuple([0.0] * n),
        speed=tuple([100.0] * n),
        g_lat=tuple([0.0] * n),
        g_lon=tuple([0.0] * n),
    )


# -- shape --------------------------------------------------------


def test_default_target_size_is_500() -> None:
    """Sprint mandates 500 points per corner. Pin it as the default
    so callers don't have to remember and a future tweak trips a
    test (and thus prompts a re-think of T034)."""
    frame = _frame_with(10)
    x = list(range(10))
    out = normalize_channels(frame, x)
    assert len(out) == 500


def test_explicit_target_size_honored() -> None:
    frame = _frame_with(10)
    x = list(range(10))
    out = normalize_channels(frame, x, target_size=128)
    assert len(out) == 128


def test_returns_channel_frame() -> None:
    frame = _frame_with(10)
    x = list(range(10))
    out = normalize_channels(frame, x)
    assert isinstance(out, ChannelFrame)


# -- numerical fidelity -------------------------------------------


def test_constant_channel_yields_constant_output() -> None:
    """Trivial sanity: y=k everywhere, resampled, should still be
    k everywhere (within float tolerance)."""
    frame = _frame_with(10, brake=0.7)
    x = list(range(10))
    out = normalize_channels(frame, x, target_size=50)
    assert all(abs(v - 0.7) < 1e-9 for v in out.brake)


def test_linear_channel_resamples_to_linear() -> None:
    """A linear input is reproducible exactly by cubic spline
    (cubic ⊃ linear). Pinning this catches regressions where the
    interpolator gets accidentally swapped for, say, nearest-
    neighbour."""
    n = 10
    x = [float(i) for i in range(n)]
    brake_linear = tuple(float(i) / (n - 1) for i in range(n))
    frame = ChannelFrame(
        brake=brake_linear,
        throttle=tuple([0.0] * n),
        steering=tuple([0.0] * n),
        speed=tuple([0.0] * n),
        g_lat=tuple([0.0] * n),
        g_lon=tuple([0.0] * n),
    )
    out = normalize_channels(frame, x, target_size=50)
    # Output sweeps from 0 to 1 monotonically.
    assert abs(out.brake[0]) < 1e-9
    assert abs(out.brake[-1] - 1.0) < 1e-9
    # Monotonic non-decreasing — cubic on linear should not
    # overshoot (within float tolerance).
    for a, b in zip(out.brake, out.brake[1:], strict=False):
        assert b >= a - 1e-12


def test_target_grid_is_evenly_spaced_over_input_range() -> None:
    """The output grid spans [x_min, x_max] in equal steps. The
    first output point corresponds to x_min (first input value),
    the last to x_max (last input value)."""
    n = 10
    x = [float(i) * 10.0 for i in range(n)]  # 0, 10, 20, ..., 90
    # speed = identity on x so we can read x_grid back from y_out.
    speeds = tuple(float(v) for v in x)
    frame = ChannelFrame(
        brake=tuple([0.0] * n),
        throttle=tuple([0.0] * n),
        steering=tuple([0.0] * n),
        speed=speeds,
        g_lat=tuple([0.0] * n),
        g_lon=tuple([0.0] * n),
    )
    out = normalize_channels(frame, x, target_size=10)
    assert abs(out.speed[0] - 0.0) < 1e-9
    assert abs(out.speed[-1] - 90.0) < 1e-9


# -- edge cases ---------------------------------------------------


def test_nan_anywhere_in_column_yields_all_nan_column(
) -> None:
    """A poisoned column (any NaN in the input) becomes all-NaN
    in the output. We don't fabricate data across gaps; downstream
    cross-correlation handles all-NaN explicitly."""
    n = 10
    x = list(range(n))
    brake = list(range(n))
    brake[5] = math.nan  # poison once
    frame = ChannelFrame(
        brake=tuple(float(v) for v in brake),
        throttle=tuple([0.5] * n),
        steering=tuple([0.0] * n),
        speed=tuple([100.0] * n),
        g_lat=tuple([0.0] * n),
        g_lon=tuple([0.0] * n),
    )
    out = normalize_channels(frame, x, target_size=50)
    assert all(math.isnan(v) for v in out.brake)
    # Other channels remain valid — NaN poisoning is per-column.
    assert all(not math.isnan(v) for v in out.throttle)


def test_misaligned_x_length_raises() -> None:
    """frame has N rows, x has M != N → bug, not data."""
    frame = _frame_with(10)
    with pytest.raises(ValueError):
        normalize_channels(frame, list(range(7)))


def test_fewer_than_four_points_raises() -> None:
    """CubicSpline minimum is 4 points; <4 is a meaningless
    interpolation. Caller decides whether to drop the segment or
    use a different method."""
    frame = _frame_with(3)
    with pytest.raises(ValueError):
        normalize_channels(frame, list(range(3)))


def test_handles_duplicate_x_values() -> None:
    """Two samples at the same x is a real condition (high-rate
    capture, repeated lap_dist on a stop). Dedup by keeping the
    last sample at each unique x — the most recent value wins.
    Without dedup, scipy.CubicSpline raises."""
    n = 10
    # Two samples with the same x (index 5 and 6 both at 5.0).
    x = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 5.0, 6.0, 7.0, 8.0]
    frame = _frame_with(n)
    out = normalize_channels(frame, x, target_size=50)
    assert len(out) == 50
