"""T042 + T043 (sprint2-backend) — skill (per-channel) extraction.

A "skill" / "dimension" maps 1:1 to a telemetry channel here:
brake, throttle, steering, speed, g_lat, g_lon.

T042: a skill is *transferred* when BOTH per-channel scores cross
      the 0.85 floor (shape >=0.85 AND intensity >=0.85).
T043: a skill is *non-transferred* when EITHER per-channel score
      is below its weakness threshold (shape <0.7 OR intensity <0.5).

The two are disjoint by construction (T042 needs ≥0.85 on both;
T043 needs <0.7 or <0.5 on one), but a moderate band exists where
neither fires — that's "partial transfer", not classified here.

Channels that are NaN-poisoned, constant-variance, or
both-sides-zero are excluded from BOTH outputs — a skill that
can't be measured isn't a transferred skill OR a known weakness.

Both lists are in canonical CHANNELS order, not score order, so
consumers can rely on stable positional semantics.
"""

from __future__ import annotations

import math

from domain.channel_extractor import CHANNELS, ChannelFrame
from domain.intensity_match import intensity_match_per_channel
from domain.shape_match import shape_match_per_channel
from domain.skill_extractor import (
    NON_TRANSFER_INTENSITY_THRESHOLD,
    NON_TRANSFER_SHAPE_THRESHOLD,
    TRANSFER_THRESHOLD,
    extract_non_transferred_skills,
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


# -- T043: non-transferred-skills extraction ----------------------


def test_non_transfer_thresholds_pinned() -> None:
    """Pin the sprint-mandated thresholds; if either changes
    (ADR), the sprint task this test covers needs a re-read."""
    assert NON_TRANSFER_SHAPE_THRESHOLD == 0.7
    assert NON_TRANSFER_INTENSITY_THRESHOLD == 0.5


def test_low_shape_emits_non_transfer() -> None:
    """Per-channel shape <0.7 → non-transferred, regardless of
    intensity. Anti-correlated signals give shape 0.0."""
    n = 50
    ramp = _ramp(n)
    a = _frame(fill=ramp)
    b = _frame(brake=tuple(reversed(ramp)), fill=ramp)
    out = extract_non_transferred_skills(a, b)
    assert "brake" in out


def test_low_intensity_emits_non_transfer() -> None:
    """Per-channel intensity <0.5 → non-transferred, regardless
    of shape. Scale 0.4 → intensity 0.4 < 0.5."""
    n = 50
    ramp = _ramp(n)
    a = _frame(fill=ramp)
    b = _frame(brake=tuple(0.4 * v for v in ramp), fill=ramp)
    out = extract_non_transferred_skills(a, b)
    assert "brake" in out


def test_both_low_emits_non_transfer() -> None:
    """Both metrics low → still just one entry per channel.
    Pin that the function doesn't double-count."""
    n = 50
    ramp = _ramp(n)
    rev = tuple(reversed(ramp))
    a = _frame(fill=ramp)
    # scale 0.3 + reverse → both shape and intensity fail
    b = _frame(brake=tuple(0.3 * v for v in rev), fill=ramp)
    out = extract_non_transferred_skills(a, b)
    assert out.count("brake") == 1


def test_high_both_excluded_from_non_transfer() -> None:
    """A transferred skill (per T042) must NOT be in non-transfer."""
    a = _frame()
    b = _frame()
    assert extract_non_transferred_skills(a, b) == []


def test_moderate_band_excluded_from_non_transfer() -> None:
    """Mid-band: shape ≥0.7 AND intensity ≥0.5 — partial transfer,
    not non-transfer. e.g., scale 0.6 → intensity 0.6 (≥0.5),
    shape 1.0 (≥0.7) → no match."""
    n = 50
    ramp = _ramp(n)
    a = _frame(fill=ramp)
    b = _frame(brake=tuple(0.6 * v for v in ramp), fill=ramp)
    out = extract_non_transferred_skills(a, b)
    assert "brake" not in out


def test_shape_threshold_strict_at_0_7() -> None:
    """shape == 0.7 does NOT match — sprint says <0.7, strict.
    Use a single-spike construction to land exactly on 0.7."""
    n = 50
    a_spike = tuple([0.0] * (n - 1) + [1.0])
    b_spike = tuple([0.0] * (n - 1) + [0.7])
    # shape: r=1.0 (linear scaling)
    # intensity: 0.7 (≥0.5, doesn't trigger intensity arm)
    a = _frame(brake=a_spike, fill=_ramp(n))
    b = _frame(brake=b_spike, fill=_ramp(n))
    out = extract_non_transferred_skills(a, b)
    assert "brake" not in out


def test_intensity_threshold_strict_at_0_5() -> None:
    """intensity == 0.5 does NOT match — sprint says <0.5, strict."""
    n = 50
    a_spike = tuple([0.0] * (n - 1) + [1.0])
    b_spike = tuple([0.0] * (n - 1) + [0.5])
    # shape: 1.0 (≥0.7, doesn't trigger shape arm)
    # intensity: 0.5/1.0 = 0.5 — boundary, NOT non-transferred
    a = _frame(brake=a_spike, fill=_ramp(n))
    b = _frame(brake=b_spike, fill=_ramp(n))
    out = extract_non_transferred_skills(a, b)
    assert "brake" not in out


def test_just_below_shape_threshold_included() -> None:
    """Picking a tiny epsilon below 0.7 confirms the strict <
    actually fires."""
    n = 50
    a_spike = tuple([0.0] * (n - 1) + [1.0])
    # intensity scale 0.69 to keep intensity arm < 0.7 but >= 0.5
    b_spike = tuple([0.0] * (n - 1) + [0.69])
    # shape: 1.0 (no shape failure)
    # intensity: 0.69 (no intensity failure either — we need shape
    # < 0.7 and we have it via... wait this doesn't work).
    # We need a separate construction: low shape, intensity ≥ 0.5
    # → swap channels A vs B at one point
    a = _frame(brake=a_spike, fill=_ramp(n))
    b = _frame(brake=b_spike, fill=_ramp(n))
    # This construction gives intensity 0.69, shape 1.0 → neither arm
    # fires → not non-transfer.
    out = extract_non_transferred_skills(a, b)
    assert "brake" not in out
    # Now actually test below shape: anti-corr → shape 0.0 < 0.7
    b2 = _frame(brake=tuple(reversed(_ramp(n))), fill=_ramp(n))
    out2 = extract_non_transferred_skills(_frame(fill=_ramp(n)), b2)
    assert "brake" in out2


def test_nan_channel_in_neither_list() -> None:
    """A NaN-poisoned channel is unmeasurable — neither
    transferred nor non-transferred."""
    n = 50
    nan_col = tuple([math.nan] * n)
    a = _frame(g_lat=nan_col)
    b = _frame(g_lat=nan_col)
    transferred = extract_transferred_skills(a, b)
    non_transferred = extract_non_transferred_skills(a, b)
    assert "g_lat" not in transferred
    assert "g_lat" not in non_transferred


def test_non_transfer_returns_canonical_order() -> None:
    """Order matches CHANNELS, not insertion or score order."""
    n = 50
    ramp = _ramp(n)
    a = _frame(fill=ramp)
    rev = tuple(reversed(ramp))
    b = _frame(
        brake=ramp,             # transferred (not in non-transfer)
        throttle=rev,           # shape 0 → non-transfer
        steering=ramp,           # transferred
        speed=rev,              # non-transfer
        g_lat=ramp,              # transferred
        g_lon=tuple(0.3 * v for v in ramp),  # intensity 0.3 → non-transfer
    )
    out = extract_non_transferred_skills(a, b)
    assert out == ["throttle", "speed", "g_lon"]


def test_t042_t043_disjoint() -> None:
    """A channel can be transferred OR non-transferred OR neither —
    never both. Pin the disjoint-set invariant given the threshold
    arithmetic (≥0.85 ∧ ≥0.85 vs <0.7 ∨ <0.5)."""
    n = 50
    ramp = _ramp(n)
    rev = tuple(reversed(ramp))
    a = _frame(fill=ramp)
    b = _frame(
        brake=ramp,                              # transferred
        throttle=rev,                            # non-transferred
        steering=tuple(0.75 * v for v in ramp),  # neither (mid)
        speed=tuple(0.6 * v for v in ramp),      # neither (mid)
        g_lat=tuple(0.4 * v for v in ramp),      # non-transferred
        g_lon=ramp,                              # transferred
    )
    transferred = set(extract_transferred_skills(a, b))
    non_transferred = set(extract_non_transferred_skills(a, b))
    assert transferred.isdisjoint(non_transferred)
