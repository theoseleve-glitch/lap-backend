"""T034 (sprint2-backend) — shape match score.

Compute a per-channel normalized cross-correlation at lag 0
between two ChannelFrames (typically the normalized output of
T033, both 500 points). Clip negatives to 0 and average across
channels with usable data. Returns a single float in [0, 1].

Why lag 0: T033 already aligned both signals onto a shared
500-point grid spanning each corner, so the comparison is
position-aligned. NCC at lag 0 = Pearson r — same formula.

Why clip negatives: physics-driven telemetry (brake, throttle,
speed) shouldn't produce anti-correlation across two attempts of
the same corner. Negative r is treated as "no shape match"
rather than "inverted match" so the [0, 1] interpretation stays
consumer-friendly.

Excluded channels (don't count toward the average):
- a NaN-poisoned column from the normalizer
- a zero-variance (constant) column — NCC is 0/0, undefined
"""

from __future__ import annotations

import math

import pytest

from domain.channel_extractor import ChannelFrame
from domain.shape_match import shape_match_score


def _frame(
    *,
    brake: tuple[float, ...],
    throttle: tuple[float, ...] | None = None,
    steering: tuple[float, ...] | None = None,
    speed: tuple[float, ...] | None = None,
    g_lat: tuple[float, ...] | None = None,
    g_lon: tuple[float, ...] | None = None,
) -> ChannelFrame:
    """Frame builder. Channels not specified default to a copy of
    `brake` so the frame is well-formed (equal lengths) and the
    test focuses on the channel under examination."""
    return ChannelFrame(
        brake=brake,
        throttle=throttle if throttle is not None else brake,
        steering=steering if steering is not None else brake,
        speed=speed if speed is not None else brake,
        g_lat=g_lat if g_lat is not None else brake,
        g_lon=g_lon if g_lon is not None else brake,
    )


def _ramp(n: int) -> tuple[float, ...]:
    """Strictly-increasing ramp 0..1, easy to reason about under
    correlation: identical ramp → r=1, reversed → r=-1."""
    return tuple(i / (n - 1) for i in range(n))


# -- score range --------------------------------------------------


def test_identical_frames_return_1_0() -> None:
    """Sprint T035 acceptance: identical sim-vs-sim sessions → 1.0.
    Pinning the upper-bound contract here so T035 can hang the
    end-to-end check on a frame round-trip."""
    a = _frame(brake=_ramp(50))
    b = _frame(brake=_ramp(50))
    assert math.isclose(shape_match_score(a, b), 1.0, abs_tol=1e-9)


def test_anti_correlated_channels_clipped_to_zero() -> None:
    """A reversed ramp gives Pearson r = -1 per channel. We clip
    to 0 (anti-correlation isn't a 'match' in this domain)."""
    a = _frame(brake=_ramp(50))
    b = _frame(brake=tuple(reversed(_ramp(50))))
    assert shape_match_score(a, b) == 0.0


def test_score_is_in_unit_interval() -> None:
    """Whatever the input, the score must land in [0, 1].
    Property test on a few plausible mixes."""
    a = _frame(brake=_ramp(50))
    b = _frame(brake=tuple(0.5 + 0.1 * v for v in _ramp(50)))
    score = shape_match_score(a, b)
    assert 0.0 <= score <= 1.0


def test_partial_correlation_scales_below_one() -> None:
    """Adding noise to one signal should drop the score below 1.0
    but stay strictly positive — the underlying ramp shape still
    correlates."""
    a = _frame(brake=_ramp(50))
    noisy = tuple(
        v + (0.1 if i % 2 == 0 else -0.1) for i, v in enumerate(_ramp(50))
    )
    b = _frame(brake=noisy)
    score = shape_match_score(a, b)
    assert 0.0 < score < 1.0


# -- channel exclusion --------------------------------------------


def test_nan_poisoned_channel_excluded_from_average() -> None:
    """A column that's all-NaN (from the T033 normalizer's
    poisoning) must be skipped, not propagate NaN into the score.
    The remaining channels still produce a real number."""
    n = 50
    nan_col = tuple([math.nan] * n)
    a = ChannelFrame(
        brake=_ramp(n),
        throttle=_ramp(n),
        steering=_ramp(n),
        speed=_ramp(n),
        g_lat=nan_col,
        g_lon=_ramp(n),
    )
    b = ChannelFrame(
        brake=_ramp(n),
        throttle=_ramp(n),
        steering=_ramp(n),
        speed=_ramp(n),
        g_lat=nan_col,
        g_lon=_ramp(n),
    )
    score = shape_match_score(a, b)
    assert math.isclose(score, 1.0, abs_tol=1e-9)


def test_zero_variance_channel_excluded_from_average() -> None:
    """A constant channel (e.g., g_lat=0 for the entire corner)
    has zero variance — Pearson r is 0/0 undefined. Must be
    skipped, not averaged in as 0."""
    n = 50
    const = tuple([0.5] * n)
    a = ChannelFrame(
        brake=_ramp(n),
        throttle=_ramp(n),
        steering=_ramp(n),
        speed=_ramp(n),
        g_lat=const,
        g_lon=_ramp(n),
    )
    b = ChannelFrame(
        brake=_ramp(n),
        throttle=_ramp(n),
        steering=_ramp(n),
        speed=_ramp(n),
        g_lat=const,
        g_lon=_ramp(n),
    )
    score = shape_match_score(a, b)
    # All other channels = 1.0 → mean = 1.0 (g_lat excluded).
    assert math.isclose(score, 1.0, abs_tol=1e-9)


def test_all_channels_excluded_returns_zero() -> None:
    """Every channel either NaN or constant → no signal to match.
    Returns 0.0 rather than NaN so the score stays consumer-safe."""
    n = 50
    const = tuple([0.5] * n)
    a = _frame(
        brake=const, throttle=const, steering=const,
        speed=const, g_lat=const, g_lon=const,
    )
    b = _frame(
        brake=const, throttle=const, steering=const,
        speed=const, g_lat=const, g_lon=const,
    )
    assert shape_match_score(a, b) == 0.0


# -- shape invariants ---------------------------------------------


def test_mismatched_lengths_raise() -> None:
    """ChannelFrames being compared must be the same length —
    typically the post-T033 normalized 500-point grid. A length
    mismatch is a programmer error, fail loud."""
    a = _frame(brake=_ramp(50))
    b = _frame(brake=_ramp(40))
    with pytest.raises(ValueError):
        shape_match_score(a, b)


def test_score_is_symmetric() -> None:
    """match(A, B) == match(B, A). Pearson r is symmetric, so
    this is essentially a free property — pinning it so a future
    'weight by A's variance' tweak doesn't silently break it."""
    a = _frame(brake=_ramp(50))
    b = _frame(brake=tuple(v * 0.5 + 0.1 for v in _ramp(50)))
    assert math.isclose(
        shape_match_score(a, b), shape_match_score(b, a), abs_tol=1e-9
    )
