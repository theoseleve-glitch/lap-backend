"""T042 (sprint2-backend) — transferred-skills extraction.

A "skill" / "dimension" maps 1:1 to a telemetry channel here:
brake, throttle, steering, speed, g_lat, g_lon. A skill is
considered *transferred* between two sessions when both per-channel
scores cross the 0.85 floor:

  per-channel shape_match >= 0.85  AND
  per-channel intensity_match >= 0.85

This task adds per-channel variants of T034 and T037 (the existing
frame-level functions stay untouched), and the T042 extractor that
composes them and applies the threshold. Channels that are
NaN-poisoned, constant-variance, or no-signal in either side are
excluded — a skill that can't be measured can't be called
transferred.

The transferred list is in canonical channel order (matching the
CHANNELS tuple), not score order — consumers can rely on stable
positional semantics.
"""

from __future__ import annotations

import math

from domain.channel_extractor import CHANNELS, ChannelFrame
from domain.intensity_match import intensity_match_per_channel
from domain.shape_match import shape_match_per_channel
from domain.skill_extractor import (
    TRANSFER_THRESHOLD,
    extract_transferred_skills,
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
    """Build a frame; channels not specified default to `fill` (or
    a 50-point ramp if no fill given). Allows independent per-
    channel scenarios."""
    default = fill if fill is not None else _ramp(50)
    return ChannelFrame(
        brake=brake if brake is not None else default,
        throttle=throttle if throttle is not None else default,
        steering=steering if steering is not None else default,
        speed=speed if speed is not None else default,
        g_lat=g_lat if g_lat is not None else default,
        g_lon=g_lon if g_lon is not None else default,
    )


# -- per-channel scoring (foundation for T042) --------------------


def test_shape_match_per_channel_returns_one_entry_per_channel() -> None:
    """All six channels valid → six entries, in canonical order."""
    a = _frame()
    b = _frame()
    out = shape_match_per_channel(a, b)
    assert list(out.keys()) == list(CHANNELS)


def test_shape_match_per_channel_skips_nan_columns() -> None:
    """NaN-poisoned channel doesn't appear — same exclusion rule
    as the frame-level shape_match_score."""
    n = 50
    nan_col = tuple([math.nan] * n)
    a = _frame(g_lat=nan_col)
    b = _frame(g_lat=nan_col)
    out = shape_match_per_channel(a, b)
    assert "g_lat" not in out
    assert "brake" in out


def test_shape_match_per_channel_skips_zero_variance() -> None:
    """Constant signal → undefined NCC → not in dict."""
    n = 50
    const = tuple([0.5] * n)
    a = _frame(brake=const)
    b = _frame(brake=const)
    out = shape_match_per_channel(a, b)
    assert "brake" not in out
    assert "throttle" in out


def test_shape_match_per_channel_clips_negative_to_zero() -> None:
    """Anti-correlated channel → r = -1, clipped to 0 — same rule
    as the frame-level function."""
    n = 50
    a = _frame(brake=_ramp(n))
    b = _frame(brake=tuple(reversed(_ramp(n))))
    out = shape_match_per_channel(a, b)
    assert out["brake"] == 0.0


def test_intensity_match_per_channel_returns_one_entry_per_channel() -> None:
    a = _frame()
    b = _frame()
    out = intensity_match_per_channel(a, b)
    assert list(out.keys()) == list(CHANNELS)


def test_intensity_match_per_channel_handles_scaling() -> None:
    """Per-channel score reflects per-channel scale: scale brake
    1.25× → 0.8, leave others identical → 1.0."""
    n = 50
    ramp = _ramp(n)
    a = _frame(fill=ramp)
    scaled_brake = tuple(1.25 * v for v in ramp)
    b = _frame(brake=scaled_brake, fill=ramp)
    out = intensity_match_per_channel(a, b)
    assert math.isclose(out["brake"], 0.8, abs_tol=1e-9)
    assert math.isclose(out["throttle"], 1.0, abs_tol=1e-9)


def test_intensity_match_per_channel_skips_both_zero_channel() -> None:
    """Channel where both sides are zero → no signal → not in dict."""
    n = 50
    zeros = tuple([0.0] * n)
    a = _frame(brake=zeros)
    b = _frame(brake=zeros)
    out = intensity_match_per_channel(a, b)
    assert "brake" not in out
    assert "throttle" in out


# -- T042: transferred-skills extraction --------------------------


def test_threshold_constant_is_0_85() -> None:
    """Pin the sprint-mandated threshold; if it changes (ADR), the
    sprint task this test covers needs a re-read."""
    assert TRANSFER_THRESHOLD == 0.85


def test_identical_sessions_transfer_all_six_skills() -> None:
    """Per-channel shape = 1.0, intensity = 1.0 everywhere → every
    skill counts as transferred. Order is canonical."""
    a = _frame()
    b = _frame()
    out = extract_transferred_skills(a, b)
    assert out == list(CHANNELS)


def test_no_skills_transfer_when_intensity_too_low() -> None:
    """Scaling every channel by 2.0 → intensity = 0.5 < 0.85 →
    no skill transfers, even though shape is identical."""
    n = 50
    ramp = _ramp(n)
    a = _frame(fill=ramp)
    b = _frame(fill=tuple(2.0 * v for v in ramp))
    assert extract_transferred_skills(a, b) == []


def test_no_skills_transfer_when_shape_too_low() -> None:
    """Reversing every channel → r = -1 → shape = 0 < 0.85 → no
    skill transfers, even though intensity is identical."""
    n = 50
    ramp = _ramp(n)
    rev = tuple(reversed(ramp))
    a = _frame(fill=ramp)
    b = _frame(fill=rev)
    assert extract_transferred_skills(a, b) == []


def test_only_channels_meeting_both_floors_are_listed() -> None:
    """Mix scenarios per-channel; only those passing both floors
    on shape AND intensity make the cut. Order matches CHANNELS."""
    n = 50
    ramp = _ramp(n)
    rev = tuple(reversed(ramp))
    a = _frame(fill=ramp)
    b = _frame(
        brake=ramp,                                    # transfer
        throttle=tuple(2.0 * v for v in ramp),         # intensity 0.5
        steering=rev,                                  # shape 0
        speed=ramp,                                    # transfer
        g_lat=tuple(0.95 * v for v in ramp),           # both ≈0.95
        g_lon=tuple(0.5 * v for v in ramp),            # intensity 0.5
    )
    assert extract_transferred_skills(a, b) == ["brake", "speed", "g_lat"]


def test_threshold_inclusive_at_0_85() -> None:
    """A channel scoring exactly 0.85 on both metrics counts as
    transferred (≥, not >). Use a single-spike construction so the
    ratio is one division (no accumulation), avoiding the float
    drift you'd get from summing scale*ramp element-wise."""
    n = 50
    spike_a = tuple([0.0] * (n - 1) + [1.0])
    spike_b = tuple([0.0] * (n - 1) + [0.85])
    # shape: linear scaling → r = 1.0
    # intensity: peak 0.85/1.0 = 0.85; integ 0.85/1.0 = 0.85; mean 0.85
    a = _frame(brake=spike_a, fill=_ramp(n))
    b = _frame(brake=spike_b, fill=_ramp(n))
    out = extract_transferred_skills(a, b)
    assert "brake" in out


def test_just_below_threshold_excluded() -> None:
    """A channel scoring just under 0.85 on intensity is excluded
    even with perfect shape."""
    n = 50
    ramp = _ramp(n)
    a = _frame(fill=ramp)
    b = _frame(brake=tuple(0.84 * v for v in ramp), fill=ramp)
    out = extract_transferred_skills(a, b)
    assert "brake" not in out


def test_nan_channel_not_transferred() -> None:
    """A NaN-poisoned channel is undefined on both metrics and so
    can't be counted as transferred."""
    n = 50
    nan_col = tuple([math.nan] * n)
    a = _frame(g_lat=nan_col)
    b = _frame(g_lat=nan_col)
    out = extract_transferred_skills(a, b)
    assert "g_lat" not in out
    # Other channels are still transferred — exclusion is per-channel.
    assert "brake" in out


def test_returns_canonical_channel_order() -> None:
    """Even if the matching channels are 'last' in CHANNELS, the
    return list is in canonical order, not score order."""
    n = 50
    ramp = _ramp(n)
    a = _frame(fill=ramp)
    b = _frame(
        brake=tuple(0.5 * v for v in ramp),       # excluded
        throttle=tuple(0.5 * v for v in ramp),    # excluded
        steering=tuple(0.5 * v for v in ramp),    # excluded
        speed=ramp,                                # transfer
        g_lat=ramp,                                # transfer
        g_lon=ramp,                                # transfer
    )
    out = extract_transferred_skills(a, b)
    assert out == ["speed", "g_lat", "g_lon"]
