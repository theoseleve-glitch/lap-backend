"""T035 + T036 (sprint2-backend) — shape match end-to-end validation.

T035 pins the upper bound: two *identical* sessions, fed through
the full bridge pipeline (T031 segment → T032 extract → T033
normalize → T034 score), produce a per-corner score of 1.0.

T036 pins the discrimination band: a synthetic "real-world"
perturbation of the sim signal (deterministic seeded noise that
emulates sensor jitter + driver-input variability) scores in
(0.6, 0.95) — positive but not perfect correlation. Until an
archived sim-vs-real telemetry pair is available, the synthetic
emulation stands in; the discrimination contract is the same.

Why integration rather than just unit:
  test_shape_match.py already covers the score function with
  hand-built ChannelFrames. That doesn't catch errors in the
  upstream stages — only this end-to-end test does.
"""

from __future__ import annotations

import math
from collections.abc import Iterator

import numpy as np

from domain.channel_extractor import extract_channels
from domain.channel_normalizer import normalize_channels
from domain.lap_segmenter import Corner, segment_paired_laps
from domain.shape_match import shape_match_score


def _synthetic_lap(
    lap_number: int, *, n_samples: int = 200
) -> Iterator[dict[str, object]]:
    """One lap of plausible telemetry across a 1000m segment.
    Channels vary smoothly with distance so cubic-spline resampling
    is well-defined; values are non-constant per channel so NCC is
    not 0/0 anywhere."""
    for i in range(n_samples):
        d = i / (n_samples - 1)  # 0..1
        yield {
            "lap_number": lap_number,
            "lap_dist_m": d * 1000.0,
            # Brake: hard at corner entry, off mid-corner.
            "brake": max(0.0, 1.0 - 4.0 * d) if d < 0.25 else 0.0,
            # Throttle: pick up exiting the corner.
            "throttle": 0.0 if d < 0.5 else (d - 0.5) * 2.0,
            # Steering: peak mid-corner.
            "steering": math.sin(d * math.pi),
            # Speed: dip then recover.
            "speed": 100.0 - 30.0 * math.sin(d * math.pi),
            # Lateral g: tracks steering.
            "g_lat": 1.5 * math.sin(d * math.pi),
            # Longitudinal g: braking then accel.
            "g_lon": -1.0 * (max(0.0, 1.0 - 4.0 * d) if d < 0.25 else 0.0)
            + 0.8 * (0.0 if d < 0.5 else (d - 0.5) * 2.0),
        }


def _identical_sessions() -> tuple[
    list[dict[str, object]], list[dict[str, object]]
]:
    """Two laps each, identical telemetry — sim-vs-sim baseline."""
    a = list(_synthetic_lap(1)) + list(_synthetic_lap(2))
    b = list(_synthetic_lap(1)) + list(_synthetic_lap(2))
    return a, b


def _score_pipeline(
    samples_a: list[dict[str, object]],
    samples_b: list[dict[str, object]],
    corners: list[Corner],
) -> list[float]:
    """Run the full T031→T034 stack, return one score per
    (lap-pair, corner) segment."""
    segments = segment_paired_laps(samples_a, samples_b, corners)
    scores: list[float] = []
    for seg in segments:
        frame_a = extract_channels(seg.samples_a)
        frame_b = extract_channels(seg.samples_b)
        # Use lap_dist_m as the x-grid for normalize.
        x_a = [
            float(s["lap_dist_m"])  # type: ignore[arg-type]
            for s in seg.samples_a
        ]
        x_b = [
            float(s["lap_dist_m"])  # type: ignore[arg-type]
            for s in seg.samples_b
        ]
        norm_a = normalize_channels(frame_a, x_a)
        norm_b = normalize_channels(frame_b, x_b)
        scores.append(shape_match_score(norm_a, norm_b))
    return scores


def test_identical_sessions_score_one_per_corner() -> None:
    """T035 acceptance: identical sim-vs-sim sessions → 1.0 per
    corner. Two laps × two corners = four segments, all 1.0."""
    a, b = _identical_sessions()
    corners = [
        Corner(name="T1", start_m=0.0, end_m=400.0),
        Corner(name="T2", start_m=500.0, end_m=900.0),
    ]
    scores = _score_pipeline(a, b, corners)
    assert len(scores) == 4
    for score in scores:
        assert math.isclose(score, 1.0, abs_tol=1e-9)


def test_identical_sessions_full_lap_corner_also_one() -> None:
    """Robustness: a single corner spanning the entire lap also
    matches at 1.0 — exercises the longest segment path."""
    a, b = _identical_sessions()
    corners = [Corner(name="full", start_m=0.0, end_m=1000.0)]
    scores = _score_pipeline(a, b, corners)
    assert len(scores) == 2  # two laps, one corner
    for score in scores:
        assert math.isclose(score, 1.0, abs_tol=1e-9)


# -- T036: sim-vs-real discrimination band ------------------------


def _real_world_emulation(
    sim_samples: list[dict[str, object]], *, seed: int = 42
) -> list[dict[str, object]]:
    """Deterministic 'real-world' perturbation of a sim sample
    stream. Models what differs between sim and real same-track
    telemetry: per-channel sensor noise + driver-input jitter.

    Magnitudes chosen empirically so the resulting score lands in
    (0.6, 0.95) — strong correlation, not perfect. When archived
    sim-vs-real data is available, swap this for the fixture.
    """
    rng = np.random.default_rng(seed=seed)
    out: list[dict[str, object]] = []
    for s in sim_samples:
        # Scale noise per-channel to the channel's natural range so
        # r drops into the target band rather than collapsing.
        out.append({
            "lap_number": s["lap_number"],
            "lap_dist_m": s["lap_dist_m"],
            "brake": max(
                0.0, float(s["brake"]) + 0.2 * float(rng.standard_normal())  # type: ignore[arg-type]
            ),
            "throttle": max(
                0.0,
                float(s["throttle"]) + 0.2 * float(rng.standard_normal()),  # type: ignore[arg-type]
            ),
            "steering": float(s["steering"])  # type: ignore[arg-type]
            + 0.2 * float(rng.standard_normal()),
            "speed": float(s["speed"])  # type: ignore[arg-type]
            + 6.0 * float(rng.standard_normal()),
            "g_lat": float(s["g_lat"])  # type: ignore[arg-type]
            + 0.3 * float(rng.standard_normal()),
            "g_lon": float(s["g_lon"])  # type: ignore[arg-type]
            + 0.25 * float(rng.standard_normal()),
        })
    return out


def test_sim_vs_real_emulation_scores_in_band() -> None:
    """T036 acceptance: the synthetic 'real' emulation of a sim
    session scores in (0.6, 0.95) — positive correlation but not
    perfect, the band that distinguishes 'same shape, different
    execution' from both 'identical' and 'unrelated'."""
    sim = list(_synthetic_lap(1)) + list(_synthetic_lap(2))
    real = _real_world_emulation(sim)
    corners = [
        Corner(name="T1", start_m=0.0, end_m=400.0),
        Corner(name="T2", start_m=500.0, end_m=900.0),
    ]
    scores = _score_pipeline(sim, real, corners)
    assert len(scores) == 4
    for score in scores:
        assert 0.6 < score < 0.95, (
            f"sim-vs-real should land in (0.6, 0.95), got {score}"
        )


def test_sim_vs_real_emulation_is_deterministic() -> None:
    """Re-running with the same seed yields the exact same score —
    pinning that the test isn't flaky."""
    sim = list(_synthetic_lap(1))
    real_a = _real_world_emulation(sim)
    real_b = _real_world_emulation(sim)
    corners = [Corner(name="full", start_m=0.0, end_m=1000.0)]
    scores_a = _score_pipeline(sim, real_a, corners)
    scores_b = _score_pipeline(sim, real_b, corners)
    assert scores_a == scores_b
