"""T049 (sprint2-backend) — partial-comparison detection.

When one or more telemetry channels are missing in either side of
a bridge pair, the bridge algorithms (T034, T037, T042, T043) all
silently skip those channels — that's the right *math*, but the
caller still needs to know it happened so the UI can render a
"partial comparison" badge and the user understands why some
skills aren't being scored.

This module returns a structured flag + the list of excluded
channels, on the same NaN-poisoning rule the score functions use:
a channel is excluded if EITHER side contains any NaN sample
(T032 fills missing values with NaN; T033 propagates any NaN to
the whole column).

Excluded channels are listed in canonical CHANNELS order so
consumers can rely on stable positional semantics.
"""

from __future__ import annotations

import math

from domain.channel_extractor import ChannelFrame
from domain.partial_comparison import (
    PARTIAL_COMPARISON_LABEL,
    PartialComparisonStatus,
    detect_partial_comparison,
)


def _ramp(n: int) -> tuple[float, ...]:
    return tuple(i / (n - 1) for i in range(n))


def _frame(
    *,
    brake: tuple[float, ...] | None = None,
    throttle: tuple[float, ...] | None = None,
    steering: tuple[float, ...] | None = None,
    speed: tuple[float, ...] | None = None,
    g_lat: tuple[float, ...] | None = None,
    g_lon: tuple[float, ...] | None = None,
    fill: tuple[float, ...] | None = None,
) -> ChannelFrame:
    default = fill if fill is not None else _ramp(50)
    return ChannelFrame(
        brake=brake if brake is not None else default,
        throttle=throttle if throttle is not None else default,
        steering=steering if steering is not None else default,
        speed=speed if speed is not None else default,
        g_lat=g_lat if g_lat is not None else default,
        g_lon=g_lon if g_lon is not None else default,
    )


# -- shape --------------------------------------------------------


def test_returns_status_dataclass() -> None:
    a = _frame()
    b = _frame()
    out = detect_partial_comparison(a, b)
    assert isinstance(out, PartialComparisonStatus)


def test_label_constant_is_partial_comparison() -> None:
    """Pin the user-facing label string so a UI/log consumer can
    match against it without re-deriving."""
    assert PARTIAL_COMPARISON_LABEL == "partial-comparison"


# -- detection ----------------------------------------------------


def test_no_missing_channels_is_not_partial() -> None:
    """All six channels valid in both sides → comparison is full."""
    a = _frame()
    b = _frame()
    out = detect_partial_comparison(a, b)
    assert out.is_partial is False
    assert out.excluded_channels == ()


def test_nan_channel_in_a_marks_partial() -> None:
    """One channel NaN-poisoned on A side → partial, that channel
    excluded. Side-asymmetric: B doesn't need to be NaN."""
    n = 50
    nan_col = tuple([math.nan] * n)
    a = _frame(g_lat=nan_col)
    b = _frame()
    out = detect_partial_comparison(a, b)
    assert out.is_partial is True
    assert out.excluded_channels == ("g_lat",)


def test_nan_channel_in_b_marks_partial() -> None:
    """Same rule on B side."""
    n = 50
    nan_col = tuple([math.nan] * n)
    a = _frame()
    b = _frame(brake=nan_col)
    out = detect_partial_comparison(a, b)
    assert out.is_partial is True
    assert out.excluded_channels == ("brake",)


def test_nan_in_both_sides_excludes_once() -> None:
    """Channel NaN in both A and B → excluded, but listed only once
    (it's still one channel)."""
    n = 50
    nan_col = tuple([math.nan] * n)
    a = _frame(g_lon=nan_col)
    b = _frame(g_lon=nan_col)
    out = detect_partial_comparison(a, b)
    assert out.is_partial is True
    assert out.excluded_channels == ("g_lon",)


def test_partial_nan_within_channel_still_marks_excluded() -> None:
    """A single NaN sample in the middle of an otherwise-valid
    channel still excludes it — same rule as T033's column
    poisoning."""
    n = 50
    half_nan = tuple(math.nan if i == 5 else float(i) for i in range(n))
    a = _frame(throttle=half_nan)
    b = _frame()
    out = detect_partial_comparison(a, b)
    assert out.is_partial is True
    assert "throttle" in out.excluded_channels


def test_multiple_missing_channels_all_listed() -> None:
    """Several channels missing → all excluded, in canonical order
    (NOT input order)."""
    n = 50
    nan_col = tuple([math.nan] * n)
    # Insert in non-canonical order to verify sort by CHANNELS.
    a = _frame(g_lat=nan_col, brake=nan_col, throttle=nan_col)
    b = _frame()
    out = detect_partial_comparison(a, b)
    assert out.is_partial is True
    assert out.excluded_channels == ("brake", "throttle", "g_lat")


def test_all_channels_missing_marks_full_partial() -> None:
    """Every channel NaN → all excluded, comparison is fully
    partial. Edge case: nothing left to compare."""
    n = 50
    nan_col = tuple([math.nan] * n)
    a = _frame(
        brake=nan_col, throttle=nan_col, steering=nan_col,
        speed=nan_col, g_lat=nan_col, g_lon=nan_col,
    )
    b = _frame()
    out = detect_partial_comparison(a, b)
    assert out.is_partial is True
    assert len(out.excluded_channels) == 6


# -- shape invariants ---------------------------------------------


def test_status_is_frozen() -> None:
    """Frozen dataclass: callers can't mutate the result accidentally."""
    a = _frame()
    b = _frame()
    out = detect_partial_comparison(a, b)
    try:
        out.is_partial = True  # type: ignore[misc]
    except (AttributeError, Exception):  # noqa: BLE001
        pass
    else:
        msg = "PartialComparisonStatus must be frozen"
        raise AssertionError(msg)
