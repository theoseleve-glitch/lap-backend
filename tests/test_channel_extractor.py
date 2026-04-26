"""T032 (sprint2-backend) — channel extraction.

Bridge analysis works on six standard channels — brake, throttle,
steering, speed, g_lat, g_lon. The extractor walks a sample stream
(typically the output of T031 PairedLapSegment.samples_a/b) and
returns a column-oriented ChannelFrame: named columns, equal-length
values. "DataFrame-shaped" without a literal pandas dependency —
T033 will add numpy when the cubic-spline resample needs it.

Missing values are NaN-filled (rather than None) so downstream
cross-correlation (T034) can detect gaps via `math.isnan` without
an extra null layer in every inner loop.
"""

from __future__ import annotations

import math
from typing import Any

from domain.channel_extractor import ChannelFrame, extract_channels


def _s(**channels: Any) -> dict[str, object]:
    """Sample stub. Pass any subset of the 6 channels by name."""
    return dict(channels)


# -- happy path ---------------------------------------------------


def test_extracts_all_six_channels_from_complete_sample() -> None:
    samples = [
        _s(brake=0.5, throttle=0.2, steering=0.1, speed=120.0,
           g_lat=0.3, g_lon=-0.1),
    ]
    frame = extract_channels(samples)
    assert frame.brake == (0.5,)
    assert frame.throttle == (0.2,)
    assert frame.steering == (0.1,)
    assert frame.speed == (120.0,)
    assert frame.g_lat == (0.3,)
    assert frame.g_lon == (-0.1,)


def test_preserves_sample_order() -> None:
    samples = [
        _s(brake=0.1, throttle=1.0, steering=0.0, speed=100.0,
           g_lat=0.0, g_lon=0.0),
        _s(brake=0.5, throttle=0.5, steering=0.0, speed=110.0,
           g_lat=0.1, g_lon=0.0),
        _s(brake=0.9, throttle=0.0, steering=0.0, speed=80.0,
           g_lat=0.4, g_lon=-0.2),
    ]
    frame = extract_channels(samples)
    assert frame.brake == (0.1, 0.5, 0.9)
    assert frame.speed == (100.0, 110.0, 80.0)


# -- missing channels ---------------------------------------------


def test_missing_channel_filled_with_nan() -> None:
    """A sample missing 'g_lat' shouldn't fail; the slot becomes
    NaN. Downstream code can `math.isnan` to detect gaps."""
    samples = [_s(brake=0.5, throttle=0.2, steering=0.0,
                  speed=100.0, g_lon=0.0)]  # g_lat missing
    frame = extract_channels(samples)
    assert math.isnan(frame.g_lat[0])
    # The other channels are unaffected.
    assert frame.brake == (0.5,)


def test_int_value_coerced_to_float() -> None:
    """Telemetry sometimes serialises ints (e.g. 0). Output is
    uniformly float so the column type is stable."""
    samples = [_s(brake=0, throttle=1, steering=0, speed=100,
                  g_lat=0, g_lon=0)]
    frame = extract_channels(samples)
    assert all(isinstance(v, float) for v in frame.brake)
    assert frame.brake == (0.0,)


def test_non_numeric_value_treated_as_missing() -> None:
    """A garbage value (string, None) → NaN, not a crash."""
    samples = [_s(brake="oops", throttle=None, steering=0.0,
                  speed=100.0, g_lat=0.0, g_lon=0.0)]
    frame = extract_channels(samples)
    assert math.isnan(frame.brake[0])
    assert math.isnan(frame.throttle[0])
    assert frame.steering == (0.0,)


# -- shape invariants ---------------------------------------------


def test_empty_input_yields_empty_frame() -> None:
    frame = extract_channels([])
    assert frame.brake == ()
    assert frame.throttle == ()
    assert frame.steering == ()
    assert frame.speed == ()
    assert frame.g_lat == ()
    assert frame.g_lon == ()


def test_all_columns_have_equal_length() -> None:
    """The defining invariant of a 'DataFrame-shaped' frame: every
    column has the same length, regardless of which channels were
    present in the input."""
    samples = [_s(brake=0.5), _s(throttle=0.5), _s()]
    frame = extract_channels(samples)
    lens = {
        len(frame.brake), len(frame.throttle), len(frame.steering),
        len(frame.speed), len(frame.g_lat), len(frame.g_lon),
    }
    assert lens == {3}


def test_returns_channel_frame_dataclass() -> None:
    frame = extract_channels([_s(brake=0.5)])
    assert isinstance(frame, ChannelFrame)


def test_channel_frame_rejects_misaligned_columns() -> None:
    """A direct ChannelFrame construction with mismatched column
    lengths is a programmer error, not data — fail loud rather
    than silently truncating downstream."""
    import pytest

    with pytest.raises(ValueError):
        ChannelFrame(
            brake=(0.5,),
            throttle=(0.2, 0.3),  # mismatched
            steering=(0.0,),
            speed=(100.0,),
            g_lat=(0.0,),
            g_lon=(0.0,),
        )


def test_channel_frame_len_returns_row_count() -> None:
    """Bonus convenience: `len(frame)` returns the row count so
    downstream code doesn't have to remember which column to ask."""
    frame = extract_channels(
        [_s(brake=0.1), _s(brake=0.2), _s(brake=0.3)]
    )
    assert len(frame) == 3
