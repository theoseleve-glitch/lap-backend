"""T120 — DataQuality scorer.

Computes a 0–100 score from three signals (per ADR-006's
`DataQuality` model):
    - channel_completeness (0..1): fraction of (sample × expected
      channel) cells filled.
    - sample_gaps_count: number of inter-sample intervals exceeding
      `gap_threshold_ms` (default 200ms).
    - sample_rate_stability: coefficient of variation of inter-
      sample dt's. 0 = perfectly stable, larger = more jitter.

Score formula (deductions from 100):
    -50 * (1 - channel_completeness)
    -5 * min(sample_gaps_count, 6)
    -100 * min(sample_rate_stability, 0.2)

The constants are pinned by these tests so any tuning of the
weights is intentional rather than silent.
"""

from domain.data_quality import DataQualityScore, score_data_quality

NS_PER_60HZ = 16_666_666  # 60Hz inter-sample interval in nanoseconds


def _samples_at_60hz(
    n: int, channels: dict[str, object] | None = None
) -> list[dict[str, object]]:
    base = channels or {}
    return [{"t_canonical_ns": i * NS_PER_60HZ, **base} for i in range(n)]


def test_perfect_session_scores_100() -> None:
    samples = _samples_at_60hz(
        10, channels={"speed_mps": 50.0, "throttle_pct": 0.5}
    )
    score = score_data_quality(
        samples, expected_channels={"speed_mps", "throttle_pct"}
    )
    assert score.score == 100
    assert score.channel_completeness == 1.0
    assert score.sample_gaps_count == 0
    assert score.sample_rate_stability == 0.0


def test_missing_channels_lower_completeness_and_score() -> None:
    # Two channels expected, only one present → completeness = 0.5
    # Score deduction = 50 * (1 - 0.5) = 25 → final 75.
    samples = _samples_at_60hz(10, channels={"speed_mps": 50.0})
    score = score_data_quality(
        samples, expected_channels={"speed_mps", "throttle_pct"}
    )
    assert score.channel_completeness == 0.5
    assert score.score == 75


def test_completeness_averages_across_samples_with_partial_channels() -> None:
    # 3 samples: first two have both channels, third has only one.
    # 5 cells filled out of 6 → completeness = 5/6.
    samples = [
        {"t_canonical_ns": 0, "speed_mps": 1.0, "throttle_pct": 0.5},
        {"t_canonical_ns": NS_PER_60HZ, "speed_mps": 2.0, "throttle_pct": 0.5},
        {"t_canonical_ns": 2 * NS_PER_60HZ, "speed_mps": 3.0},
    ]
    score = score_data_quality(
        samples, expected_channels={"speed_mps", "throttle_pct"}
    )
    assert abs(score.channel_completeness - 5 / 6) < 1e-9


def test_no_expected_channels_yields_completeness_one() -> None:
    # Vacuous: zero expectations → 100% met. Don't divide-by-zero.
    samples = _samples_at_60hz(3, channels={})
    score = score_data_quality(samples, expected_channels=set())
    assert score.channel_completeness == 1.0


def test_large_gap_increments_gaps_count() -> None:
    samples = [
        {"t_canonical_ns": 0},
        {"t_canonical_ns": NS_PER_60HZ},  # 16.67ms — not a gap
        {"t_canonical_ns": NS_PER_60HZ + 500_000_000},  # 500ms gap
    ]
    score = score_data_quality(samples, expected_channels=set())
    assert score.sample_gaps_count == 1


def test_intervals_below_threshold_are_not_gaps() -> None:
    samples = [
        {"t_canonical_ns": 0},
        {"t_canonical_ns": 100_000_000},  # 100ms — under 200ms default
        {"t_canonical_ns": 200_000_000},
    ]
    score = score_data_quality(samples, expected_channels=set())
    assert score.sample_gaps_count == 0


def test_each_gap_costs_5_points_up_to_6_gaps() -> None:
    # 8 gaps but penalty caps at 6 → 30 points off.
    intervals_ns = [500_000_000] * 8  # all 500ms = gaps
    samples: list[dict[str, object]] = [{"t_canonical_ns": 0}]
    t = 0
    for dt in intervals_ns:
        t += dt
        samples.append({"t_canonical_ns": t})
    score = score_data_quality(samples, expected_channels=set())
    assert score.sample_gaps_count == 8
    # Channels and rate_stability are clean (uniform dts), so:
    # score = 100 - 5 * min(8, 6) = 100 - 30 = 70.
    assert score.score == 70


def test_unstable_rate_yields_high_cv() -> None:
    samples = [
        {"t_canonical_ns": 0},
        {"t_canonical_ns": 10_000_000},
        {"t_canonical_ns": 40_000_000},
        {"t_canonical_ns": 50_000_000},
        {"t_canonical_ns": 80_000_000},
    ]
    score = score_data_quality(samples, expected_channels=set())
    assert score.sample_rate_stability > 0.1


def test_stable_rate_yields_zero_cv() -> None:
    samples = _samples_at_60hz(20)
    score = score_data_quality(samples, expected_channels=set())
    assert score.sample_rate_stability == 0.0


def test_empty_sample_stream_scores_zero() -> None:
    score = score_data_quality([], expected_channels={"speed_mps"})
    assert score.score == 0
    assert score.sample_gaps_count == 0


def test_pathologically_bad_session_scores_zero() -> None:
    # No channels (50pt) + ≥6 gaps (30pt cap) + jittery rate (20pt cap)
    # = 100pt deductions → score floors at 0.
    intervals_ns = [
        500_000_000,
        1_000_000_000,
        300_000_000,
        800_000_000,
        400_000_000,
        1_500_000_000,
        250_000_000,
        900_000_000,
    ]
    samples: list[dict[str, object]] = [{"t_canonical_ns": 0}]
    t = 0
    for dt in intervals_ns:
        t += dt
        samples.append({"t_canonical_ns": t})
    score = score_data_quality(samples, expected_channels={"speed_mps"})
    # Score floors at 0; can't go negative.
    assert score.score == 0


def test_returns_data_quality_score_dataclass() -> None:
    samples = _samples_at_60hz(3)
    score = score_data_quality(samples, expected_channels=set())
    assert isinstance(score, DataQualityScore)
