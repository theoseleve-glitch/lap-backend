"""T119 — Session metric extraction.

Computes the four session-level scalars the sprint backlog calls
out: best lap, average lap, consistency (stdev of lap times), and
peak speed. Pure function over `LapRecord`s and a sample stream;
T122 will pick up persistence into the `SessionRecord`.

Lap-derived metrics consider only laps where `valid=True` (i.e.
laps T117 didn't flag as physically implausible). Peak speed is
the max `speed_mps` field across all samples, converted to kph
and rounded to 2 decimal places.
"""

from domain.lap_detection import LapRecord
from domain.session_metrics import SessionMetrics, extract_session_metrics


def _lap(lap_number: int, time_ms: int, valid: bool = True) -> LapRecord:
    return LapRecord(lap_number=lap_number, time_ms=time_ms, valid=valid)


def test_best_lap_is_minimum_of_valid_lap_times() -> None:
    laps = [_lap(1, 90_500), _lap(2, 89_200), _lap(3, 90_100)]
    metrics = extract_session_metrics(laps, samples=[])
    assert metrics.best_lap_ms == 89_200


def test_invalid_laps_excluded_from_best_lap() -> None:
    # The "fastest" lap is actually invalid (under-1-minute glitch);
    # we want the next-fastest VALID lap reported.
    laps = [
        _lap(1, 500, valid=False),  # implausibly short — outlap glitch
        _lap(2, 89_200),
        _lap(3, 90_100),
    ]
    metrics = extract_session_metrics(laps, samples=[])
    assert metrics.best_lap_ms == 89_200


def test_avg_lap_is_mean_of_valid_lap_times() -> None:
    laps = [_lap(1, 90_000), _lap(2, 92_000), _lap(3, 91_000)]
    metrics = extract_session_metrics(laps, samples=[])
    assert metrics.avg_lap_ms == 91_000


def test_consistency_is_sample_stdev_of_valid_lap_times() -> None:
    # Three laps at 89s, 90s, 91s. Mean = 90s.
    # Sample variance = ((89-90)^2 + (90-90)^2 + (91-90)^2) / 2 = 1.0 s^2
    # Sample stdev = 1.0s = 1000 ms.
    laps = [_lap(1, 89_000), _lap(2, 90_000), _lap(3, 91_000)]
    metrics = extract_session_metrics(laps, samples=[])
    assert metrics.consistency_ms == 1000


def test_consistency_is_none_for_a_single_valid_lap() -> None:
    # Sample stdev needs n >= 2; one lap carries no spread information.
    laps = [_lap(1, 90_000)]
    metrics = extract_session_metrics(laps, samples=[])
    assert metrics.consistency_ms is None


def test_invalid_laps_excluded_from_consistency() -> None:
    # The wild lap is invalid; only 89_000 and 91_000 enter the calc.
    laps = [
        _lap(1, 89_000),
        _lap(2, 999_999, valid=False),
        _lap(3, 91_000),
    ]
    metrics = extract_session_metrics(laps, samples=[])
    # Stdev of {89000, 91000}: mean=90000, var=2_000_000, stdev≈1414.21
    assert metrics.consistency_ms == 1414


def test_peak_speed_is_max_speed_mps_converted_to_kph() -> None:
    samples = [
        {"speed_mps": 50.0},  # 180 kph
        {"speed_mps": 75.0},  # 270 kph
        {"speed_mps": 60.0},  # 216 kph
    ]
    metrics = extract_session_metrics(laps=[], samples=samples)
    assert metrics.peak_speed_kph == 270.0


def test_peak_speed_is_rounded_to_two_decimal_places() -> None:
    samples = [{"speed_mps": 11.111}]  # 39.9996 kph → 40.0
    metrics = extract_session_metrics(laps=[], samples=samples)
    assert metrics.peak_speed_kph == 40.0


def test_peak_speed_is_none_when_no_samples_carry_speed_mps() -> None:
    # Samples with other fields but no speed_mps shouldn't fabricate a 0.
    samples = [{"lap_number": 1}, {"throttle_pct": 0.5}]
    metrics = extract_session_metrics(laps=[], samples=samples)
    assert metrics.peak_speed_kph is None


def test_no_valid_laps_yields_none_for_lap_metrics() -> None:
    # All laps invalid (or empty) → best/avg/consistency are None.
    laps = [_lap(1, 500, valid=False), _lap(2, 999_999, valid=False)]
    metrics = extract_session_metrics(laps, samples=[])
    assert metrics.best_lap_ms is None
    assert metrics.avg_lap_ms is None
    assert metrics.consistency_ms is None


def test_returns_session_metrics_dataclass_instance() -> None:
    metrics = extract_session_metrics(laps=[], samples=[])
    assert isinstance(metrics, SessionMetrics)
