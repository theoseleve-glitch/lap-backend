"""T044 (sprint2-backend) — bridge algorithm performance.

End-to-end benchmark: a realistic pair of sessions (10 laps,
2000 samples each) fed through the full bridge stack — T031
segment → T032 extract → T033 normalize → T034 shape +
T037 intensity → T039/T040 divergences → T042/T043 skill
extraction — must finish in under 5 seconds wall-clock per pair.

Why a wall-clock bound rather than asymptotics: the bridge
endpoint runs synchronously per request; a 5s ceiling is the
product budget for the full algo before users perceive lag.
A future regression (e.g., O(N²) accidentally introduced in
normalize) is caught here with the cleanest signal — wall time —
even if the algorithmic explanation lands in code review.

The 5s budget is intentionally generous (real workloads land
~100ms locally) so this test isn't flaky on slower CI hardware.
If you need to trace a perceived slowdown, run with -s and the
test prints elapsed time before asserting.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterator

from domain.bridge_divergence import (
    CornerScore,
    detect_intensity_divergences,
    detect_shape_divergences,
)
from domain.channel_extractor import extract_channels
from domain.channel_normalizer import normalize_channels
from domain.intensity_match import intensity_match_score
from domain.lap_segmenter import Corner, segment_paired_laps
from domain.shape_match import shape_match_score
from domain.skill_extractor import (
    extract_non_transferred_skills,
    extract_transferred_skills,
)

PERFORMANCE_BUDGET_SECONDS = 5.0


def _synthetic_session(
    n_laps: int, samples_per_lap: int
) -> Iterator[dict[str, object]]:
    """Plausible session telemetry: smooth varying channels over a
    1000m segment, repeated N laps. Identical structure to the
    pipeline test but scaled to realistic volumes."""
    for lap in range(1, n_laps + 1):
        for i in range(samples_per_lap):
            d = i / (samples_per_lap - 1)
            yield {
                "lap_number": lap,
                "lap_dist_m": d * 1000.0,
                "brake": max(0.0, 1.0 - 4.0 * d) if d < 0.25 else 0.0,
                "throttle": 0.0 if d < 0.5 else (d - 0.5) * 2.0,
                "steering": math.sin(d * math.pi),
                "speed": 100.0 - 30.0 * math.sin(d * math.pi),
                "g_lat": 1.5 * math.sin(d * math.pi),
                "g_lon": (
                    -1.0
                    * (max(0.0, 1.0 - 4.0 * d) if d < 0.25 else 0.0)
                    + 0.8 * (0.0 if d < 0.5 else (d - 0.5) * 2.0)
                ),
            }


def _process_pair(
    samples_a: list[dict[str, object]],
    samples_b: list[dict[str, object]],
    corners: list[Corner],
) -> tuple[list, list, list, list]:
    """Run the full bridge stack inline and return the four
    consumer outputs (divergences × 2 + skills × 2)."""
    segments = segment_paired_laps(samples_a, samples_b, corners)
    corner_scores: list[CornerScore] = []
    transferred_per_segment: list[list[str]] = []
    non_transferred_per_segment: list[list[str]] = []
    for seg in segments:
        frame_a = extract_channels(seg.samples_a)
        frame_b = extract_channels(seg.samples_b)
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
        corner_scores.append(
            CornerScore(
                corner_name=seg.corner_name,
                shape_score=shape_match_score(norm_a, norm_b),
                intensity_score=intensity_match_score(norm_a, norm_b),
            )
        )
        transferred_per_segment.append(
            extract_transferred_skills(norm_a, norm_b)
        )
        non_transferred_per_segment.append(
            extract_non_transferred_skills(norm_a, norm_b)
        )
    intensity_divs = detect_intensity_divergences(corner_scores)
    shape_divs = detect_shape_divergences(corner_scores)
    return (
        intensity_divs,
        shape_divs,
        transferred_per_segment,
        non_transferred_per_segment,
    )


def test_realistic_session_pair_processes_under_budget() -> None:
    """A 10-lap × 2000-sample pair, 5 corners → 50 segments. Must
    complete the full T031→T043 pipeline in under PERFORMANCE_BUDGET_SECONDS.
    Sized to be representative of a track-day session run; enough
    work that a regression with bad asymptotics would visibly blow
    the budget."""
    a = list(_synthetic_session(n_laps=10, samples_per_lap=2000))
    b = list(_synthetic_session(n_laps=10, samples_per_lap=2000))
    corners = [
        Corner(name="T1", start_m=0.0, end_m=200.0),
        Corner(name="T2", start_m=200.0, end_m=400.0),
        Corner(name="T3", start_m=400.0, end_m=600.0),
        Corner(name="T4", start_m=600.0, end_m=800.0),
        Corner(name="T5", start_m=800.0, end_m=1000.0),
    ]

    t0 = time.perf_counter()
    intensity_divs, shape_divs, transferred, non_transferred = (
        _process_pair(a, b, corners)
    )
    elapsed = time.perf_counter() - t0

    # Sanity: the pipeline produced something — guards against a
    # future "early return" that would technically clear the budget
    # but skip all the work.
    assert len(transferred) == 50  # 10 laps × 5 corners
    assert len(non_transferred) == 50
    # Identical sessions: zero divergences expected.
    assert intensity_divs == []
    assert shape_divs == []

    print(f"\n[T044] bridge pair processed in {elapsed:.3f}s")
    assert elapsed < PERFORMANCE_BUDGET_SECONDS, (
        f"bridge pair processing took {elapsed:.3f}s, "
        f"budget is {PERFORMANCE_BUDGET_SECONDS}s"
    )
