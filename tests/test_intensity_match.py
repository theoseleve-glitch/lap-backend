"""T037 (sprint2-backend) — intensity match score.

Compute a per-channel magnitude similarity between two
ChannelFrames (typically the post-T033 normalized 500-point
output). For each channel, we average two ratios:

  peak_ratio = min(|peak_a|, |peak_b|) / max(|peak_a|, |peak_b|)
  integ_ratio = min(sum|a|, sum|b|) / max(sum|a|, sum|b|)

Both are in [0, 1]: 1 = identical magnitude, lower = bigger gap.
The channel score is (peak_ratio + integ_ratio) / 2; the frame
score is the mean across valid channels.

Why peak AND integral, not just one:
  Two laps can have the same peak brake pressure (driver always
  hits 100%) but very different integrals (one taps, one holds
  through the corner). Peak alone misses that. Integral alone
  blurs a sharp spike vs a long mild push.

Why absolute values:
  g_lat / g_lon are signed (cornering direction). For magnitude
  comparison, sign is irrelevant — abs() captures "how hard"
  without "which way".

Excluded channels (don't count toward the average):
- a NaN-poisoned column (from T033 normalizer)
- a channel where both sides are entirely zero (no signal to
  compare; one zero + one non-zero is *not* skipped — that's a
  meaningful 0.0 difference)
"""

from __future__ import annotations

import math

import pytest

from domain.channel_extractor import ChannelFrame
from domain.intensity_match import intensity_match_score


def _frame(
    *,
    brake: tuple[float, ...],
    throttle: tuple[float, ...] | None = None,
    steering: tuple[float, ...] | None = None,
    speed: tuple[float, ...] | None = None,
    g_lat: tuple[float, ...] | None = None,
    g_lon: tuple[float, ...] | None = None,
) -> ChannelFrame:
    return ChannelFrame(
        brake=brake,
        throttle=throttle if throttle is not None else brake,
        steering=steering if steering is not None else brake,
        speed=speed if speed is not None else brake,
        g_lat=g_lat if g_lat is not None else brake,
        g_lon=g_lon if g_lon is not None else brake,
    )


def _ramp(n: int) -> tuple[float, ...]:
    return tuple(i / (n - 1) for i in range(n))


# -- score range --------------------------------------------------


def test_identical_frames_return_1_0() -> None:
    """Sprint T038 acceptance upper-bound: identical sessions → 1.0.
    Pinning here so T038 can hang the round-trip on this contract."""
    a = _frame(brake=_ramp(50))
    b = _frame(brake=_ramp(50))
    assert math.isclose(intensity_match_score(a, b), 1.0, abs_tol=1e-9)


def test_doubled_signal_scores_zero_point_five() -> None:
    """If b = 2*a everywhere, peak_ratio = 0.5 and integ_ratio = 0.5
    on every channel. Mean = 0.5. Provides intuition for the formula
    and catches sign-handling regressions."""
    n = 50
    a = _frame(brake=_ramp(n))
    b = _frame(brake=tuple(2.0 * v for v in _ramp(n)))
    assert math.isclose(
        intensity_match_score(a, b), 0.5, abs_tol=1e-9
    )


def test_one_point_two_five_scaling_scores_0_8() -> None:
    """Sprint T038 acceptance lower-bound: a 1.25× scale gives 0.8.
    Pinning the relationship between scale factor and score so a
    formula tweak is caught here, not in the integration test."""
    n = 50
    a = _frame(brake=_ramp(n))
    b = _frame(brake=tuple(1.25 * v for v in _ramp(n)))
    assert math.isclose(
        intensity_match_score(a, b), 0.8, abs_tol=1e-9
    )


def test_score_is_in_unit_interval() -> None:
    a = _frame(brake=_ramp(50))
    b = _frame(brake=tuple(0.3 * v + 0.05 for v in _ramp(50)))
    score = intensity_match_score(a, b)
    assert 0.0 <= score <= 1.0


# -- sign / magnitude semantics -----------------------------------


def test_sign_flipped_channel_is_a_perfect_intensity_match() -> None:
    """g_lat = +1.5 (left cornering) vs g_lat = -1.5 (right
    cornering) have the same magnitude. Intensity is sign-agnostic
    by design; shape_match handles the directional comparison."""
    n = 50
    pos = tuple([1.5] * n)
    neg = tuple([-1.5] * n)
    # Need a non-constant channel elsewhere so we have at least one
    # valid (non-zero) channel to average — but g_lat itself is
    # what we're checking.
    a = ChannelFrame(
        brake=_ramp(n),
        throttle=_ramp(n),
        steering=_ramp(n),
        speed=_ramp(n),
        g_lat=pos,
        g_lon=_ramp(n),
    )
    b = ChannelFrame(
        brake=_ramp(n),
        throttle=_ramp(n),
        steering=_ramp(n),
        speed=_ramp(n),
        g_lat=neg,
        g_lon=_ramp(n),
    )
    # All other channels = 1.0; g_lat = 1.0 too (|+1.5| == |-1.5|).
    assert math.isclose(
        intensity_match_score(a, b), 1.0, abs_tol=1e-9
    )


# -- channel exclusion --------------------------------------------


def test_nan_poisoned_channel_excluded_from_average() -> None:
    """Same NaN-poisoning rule as shape_match: a column with any
    NaN in either side is skipped, not propagated."""
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
    score = intensity_match_score(a, b)
    assert math.isclose(score, 1.0, abs_tol=1e-9)


def test_both_zero_channel_excluded_from_average() -> None:
    """A channel where both sides are entirely zero (e.g., no
    braking on a flat-out corner) carries no comparable signal —
    skip rather than counting as a 'perfect match', so the average
    reflects only channels that actually moved."""
    n = 50
    zeros = tuple([0.0] * n)
    a = ChannelFrame(
        brake=zeros,  # no signal in either side
        throttle=_ramp(n),
        steering=_ramp(n),
        speed=_ramp(n),
        g_lat=_ramp(n),
        g_lon=_ramp(n),
    )
    b = ChannelFrame(
        brake=zeros,
        throttle=tuple(2.0 * v for v in _ramp(n)),  # 0.5
        steering=tuple(2.0 * v for v in _ramp(n)),
        speed=tuple(2.0 * v for v in _ramp(n)),
        g_lat=tuple(2.0 * v for v in _ramp(n)),
        g_lon=tuple(2.0 * v for v in _ramp(n)),
    )
    # If brake were counted as 1.0, mean = (1.0 + 5*0.5)/6 ≈ 0.583.
    # With brake skipped, mean = 5*0.5 / 5 = 0.5 — pinning the skip.
    assert math.isclose(
        intensity_match_score(a, b), 0.5, abs_tol=1e-9
    )


def test_one_zero_one_nonzero_channel_is_not_skipped() -> None:
    """Asymmetric: brake=0 in one session and braking hard in the
    other is a meaningful intensity difference — score 0 for that
    channel, not skipped."""
    n = 50
    zeros = tuple([0.0] * n)
    nonzero = _ramp(n)
    a = ChannelFrame(
        brake=zeros,
        throttle=_ramp(n),
        steering=_ramp(n),
        speed=_ramp(n),
        g_lat=_ramp(n),
        g_lon=_ramp(n),
    )
    b = ChannelFrame(
        brake=nonzero,
        throttle=_ramp(n),
        steering=_ramp(n),
        speed=_ramp(n),
        g_lat=_ramp(n),
        g_lon=_ramp(n),
    )
    # Brake: peak_a=0, peak_b>0 → peak_ratio=0; integ same → 0.
    # Channel score = 0. Other 5 channels = 1.0.
    # Mean = 5/6 ≈ 0.8333.
    expected = 5.0 / 6.0
    assert math.isclose(
        intensity_match_score(a, b), expected, abs_tol=1e-9
    )


def test_all_zero_channels_returns_zero() -> None:
    """Every channel zero in both sides → no comparable signal at
    all → 0.0 (consumer-safe, never NaN)."""
    n = 50
    zeros = tuple([0.0] * n)
    a = _frame(
        brake=zeros, throttle=zeros, steering=zeros,
        speed=zeros, g_lat=zeros, g_lon=zeros,
    )
    b = _frame(
        brake=zeros, throttle=zeros, steering=zeros,
        speed=zeros, g_lat=zeros, g_lon=zeros,
    )
    assert intensity_match_score(a, b) == 0.0


# -- shape invariants ---------------------------------------------


def test_mismatched_lengths_raise() -> None:
    a = _frame(brake=_ramp(50))
    b = _frame(brake=_ramp(40))
    with pytest.raises(ValueError):
        intensity_match_score(a, b)


def test_score_is_symmetric() -> None:
    """match(A, B) == match(B, A). min/max ratios are symmetric;
    pinning so a future weighting tweak doesn't silently break it."""
    a = _frame(brake=_ramp(50))
    b = _frame(brake=tuple(0.5 * v + 0.1 for v in _ramp(50)))
    assert math.isclose(
        intensity_match_score(a, b),
        intensity_match_score(b, a),
        abs_tol=1e-9,
    )
