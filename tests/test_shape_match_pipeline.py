"""T035 (sprint2-backend) — shape match end-to-end validation.

Pins the contract that two *identical* sessions, fed through the
full bridge pipeline (T031 segment → T032 extract → T033 normalize
→ T034 score), produce a per-corner score of 1.0.

This is the sim-vs-sim baseline. If a later refactor accidentally
breaks the round-trip (e.g., normalize introduces float drift, or
segment loses the last sample), this test catches it.

Why integration rather than just unit:
  test_shape_match.py already covers the score function with
  hand-built ChannelFrames. That doesn't catch errors in the
  upstream stages — only this end-to-end test does.
"""

from __future__ import annotations

import math
from collections.abc import Iterator

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
