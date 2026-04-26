"""T035 + T036 + T038 (sprint2-backend) — bridge pipeline validation.

End-to-end checks that the full T031→T034 / T031→T037 stacks
behave correctly on synthetic sessions:

T035 — shape match upper bound: identical sessions → 1.0 per corner.
T036 — shape match band: synthetic 'real-world' perturbation of a
       sim session lands in (0.6, 0.95) — positive correlation but
       not perfect. Stand-in until an archived sim-vs-real pair is
       available; the discrimination contract is the same.
T038 — intensity match upper + scaled bound: identical sessions →
       1.0; a uniform 1.25× scale on every channel → 0.8 per corner
       (1/1.25 = 0.8 — the algebraic relationship is the contract).

Why integration rather than just unit:
  test_shape_match.py and test_intensity_match.py already cover
  the score functions on hand-built ChannelFrames. That doesn't
  catch errors in the upstream stages — only end-to-end does.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator

import numpy as np

from domain.channel_extractor import ChannelFrame, extract_channels
from domain.channel_normalizer import normalize_channels
from domain.intensity_match import intensity_match_score
from domain.lap_segmenter import Corner, segment_paired_laps
from domain.shape_match import shape_match_score

ScoreFn = Callable[[ChannelFrame, ChannelFrame], float]


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
    score_fn: ScoreFn = shape_match_score,
) -> list[float]:
    """Run the full T031→T033 stack and apply `score_fn` (T034
    shape_match by default, T037 intensity_match for T038), return
    one score per (lap-pair, corner) segment."""
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
        scores.append(score_fn(norm_a, norm_b))
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


# -- T038: intensity match end-to-end -----------------------------


_NUMERIC_KEYS = ("brake", "throttle", "steering", "speed", "g_lat", "g_lon")


def _scale_session(
    sim_samples: list[dict[str, object]], factor: float
) -> list[dict[str, object]]:
    """Multiply every numeric channel by `factor`. Lap and distance
    keys are preserved unchanged so segmentation still aligns."""
    out: list[dict[str, object]] = []
    for s in sim_samples:
        scaled = dict(s)
        for k in _NUMERIC_KEYS:
            scaled[k] = factor * float(s[k])  # type: ignore[arg-type]
        out.append(scaled)
    return out


def test_identical_sessions_intensity_score_one_per_corner() -> None:
    """T038 acceptance upper-bound: identical sim-vs-sim sessions
    score 1.0 on intensity match, just as on shape match."""
    a, b = _identical_sessions()
    corners = [
        Corner(name="T1", start_m=0.0, end_m=400.0),
        Corner(name="T2", start_m=500.0, end_m=900.0),
    ]
    scores = _score_pipeline(a, b, corners, score_fn=intensity_match_score)
    assert len(scores) == 4
    for score in scores:
        assert math.isclose(score, 1.0, abs_tol=1e-9)


def test_uniformly_scaled_session_intensity_score_is_inverse() -> None:
    """T038 acceptance scaled-bound: a session multiplied uniformly
    by 1.25 scores 0.8 on intensity match (1/1.25 = 0.8). Pins the
    algebraic relationship between scale factor and score."""
    sim = list(_synthetic_lap(1)) + list(_synthetic_lap(2))
    scaled = _scale_session(sim, factor=1.25)
    corners = [
        Corner(name="T1", start_m=0.0, end_m=400.0),
        Corner(name="T2", start_m=500.0, end_m=900.0),
    ]
    scores = _score_pipeline(
        sim, scaled, corners, score_fn=intensity_match_score
    )
    assert len(scores) == 4
    for score in scores:
        assert math.isclose(score, 0.8, abs_tol=1e-9)


def test_intensity_score_is_independent_of_scale_direction() -> None:
    """Robustness: scaling A up vs. scaling B up gives the same
    score — symmetry through the pipeline. Catches regressions
    where a one-sided abs() or order-dependent op leaks in."""
    sim = list(_synthetic_lap(1))
    scaled = _scale_session(sim, factor=1.25)
    corners = [Corner(name="full", start_m=0.0, end_m=1000.0)]
    a = _score_pipeline(sim, scaled, corners, score_fn=intensity_match_score)
    b = _score_pipeline(scaled, sim, corners, score_fn=intensity_match_score)
    assert a == b
