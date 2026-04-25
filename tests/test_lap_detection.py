"""T117 — Lap detection from samples + TrackLayout.

The detection algorithm walks samples in order, groups them by
`lap_number`, and emits a `LapRecord` for each completed lap (i.e.
each lap with a known transition both in and out — the in-lap and
the final lap-in-progress are excluded).

Track length is currently advisory: it lets us mark physically
impossible lap times (a 30-second lap of a 7km circuit) as
`valid=False` without rejecting them outright, since downstream
analytics need to see invalid laps too.
"""

from domain.lap_detection import LapRecord, detect_laps
from domain.track_layout import TrackLayout

SPA = TrackLayout(name="spa-gp", length_m=7004.0)
DAY_MS = 24 * 60 * 60 * 1000


def _sample(lap_number: int, t_ms: int, **rest: object) -> dict[str, object]:
    return {
        "t_canonical_ns": t_ms * 1_000_000,
        "t_source_ns": t_ms * 1_000_000,
        "t_source_clock": "irsdk_tick",
        "lap_number": lap_number,
        **rest,
    }


def test_detects_two_complete_laps_from_three_lap_run() -> None:
    # Three lap_number values across the stream → laps 1 and 2 are
    # both complete (have a successor); lap 3 is in-progress and is
    # therefore not emitted.
    samples = [
        _sample(1, 0),
        _sample(1, 60_000),
        _sample(2, 90_000),  # transition into lap 2 at 90s
        _sample(2, 150_000),
        _sample(3, 180_000),  # transition into lap 3 at 180s
        _sample(3, 240_000),
    ]
    laps = list(detect_laps(samples, SPA))
    assert [lap.lap_number for lap in laps] == [1, 2]


def test_lap_time_is_difference_between_transitions_in_ms() -> None:
    samples = [
        _sample(1, 0),
        _sample(2, 90_000),  # 90s after lap 1 start
        _sample(3, 180_000),  # 90s after lap 2 start
    ]
    laps = list(detect_laps(samples, SPA))
    assert [lap.time_ms for lap in laps] == [90_000, 90_000]


def test_in_progress_lap_is_not_emitted() -> None:
    # Single lap with no successor → no LapRecord. We don't know its
    # duration yet.
    samples = [_sample(1, 0), _sample(1, 60_000)]
    assert list(detect_laps(samples, SPA)) == []


def test_empty_sample_stream_yields_no_laps() -> None:
    assert list(detect_laps([], SPA)) == []


def test_missing_lap_number_field_is_skipped() -> None:
    # If a sensor doesn't carry lap_number on every sample, those
    # samples are passed over rather than treated as lap 0.
    samples = [
        _sample(1, 0),
        {"t_canonical_ns": 30_000_000_000, "t_source_clock": "gnss"},
        _sample(2, 60_000),
        _sample(3, 120_000),
    ]
    laps = list(detect_laps(samples, SPA))
    assert [lap.lap_number for lap in laps] == [1, 2]


def test_lap_record_carries_valid_true_for_normal_laps() -> None:
    samples = [_sample(1, 0), _sample(2, 60_000)]
    [lap] = list(detect_laps(samples, SPA))
    assert lap.valid is True
    assert lap.time_ms == 60_000


def test_unrealistically_short_lap_is_marked_invalid() -> None:
    # A 1-second lap of Spa is physically impossible. The detector
    # still emits the record so downstream sees it, but flags valid=False.
    samples = [_sample(1, 0), _sample(2, 1_000)]
    [lap] = list(detect_laps(samples, SPA))
    assert lap.valid is False


def test_unrealistically_long_lap_is_marked_invalid() -> None:
    # Anything longer than a day is almost certainly a session pause
    # / clock skew rather than a real lap.
    samples = [_sample(1, 0), _sample(2, DAY_MS + 1)]
    [lap] = list(detect_laps(samples, SPA))
    assert lap.valid is False


def test_non_monotonic_lap_number_does_not_create_phantom_laps() -> None:
    # Some sims briefly report the previous lap's number after a
    # lap transition (race-state quirk). Treat repeated values as
    # the same lap, not a regression.
    samples = [
        _sample(1, 0),
        _sample(1, 30_000),
        _sample(2, 60_000),
        _sample(2, 65_000),
        _sample(2, 90_000),
        _sample(3, 120_000),
    ]
    laps = list(detect_laps(samples, SPA))
    assert [lap.lap_number for lap in laps] == [1, 2]


def test_returns_lap_record_dataclass_instances() -> None:
    samples = [_sample(1, 0), _sample(2, 60_000)]
    [lap] = list(detect_laps(samples, SPA))
    assert isinstance(lap, LapRecord)
    assert lap.lap_number == 1
