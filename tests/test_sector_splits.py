"""T118 — Sector splits from samples + TrackLayout checkpoints.

Boundaries are stored on TrackLayout as `sector_boundaries_m`: a
strictly-increasing tuple of distances (in metres from start) where
sectors transition. Two boundaries → three sectors. The algorithm
walks samples in order, watching each sample's `lap_dist_m` cross
the next boundary, and emits a `SectorRecord` per completed sector.

The final sector of a lap closes when the *next* lap's first sample
arrives — so as with lap detection, the in-progress final lap of
the stream emits no sectors.
"""

from domain.sector_splits import SectorRecord, compute_sector_times
from domain.track_layout import TrackLayout

# A 3-sector layout: 0..1800m = S0, 1800..4200m = S1, 4200..end = S2.
SPA_3SEC = TrackLayout(
    name="spa-gp",
    length_m=7004.0,
    sector_boundaries_m=(1800.0, 4200.0),
)


def _sample(
    lap_number: int,
    t_ms: int,
    lap_dist_m: float | None,
) -> dict[str, object]:
    out: dict[str, object] = {
        "t_canonical_ns": t_ms * 1_000_000,
        "t_source_ns": t_ms * 1_000_000,
        "t_source_clock": "irsdk_tick",
        "lap_number": lap_number,
    }
    if lap_dist_m is not None:
        out["lap_dist_m"] = lap_dist_m
    return out


def test_three_sectors_emitted_for_complete_lap() -> None:
    # Lap 1: cross 1800m at t=30s, 4200m at t=70s. Lap 2 starts at
    # t=100s. → S0 = 30s, S1 = 40s, S2 = 30s.
    samples = [
        _sample(1, 0, 0.0),
        _sample(1, 30_000, 1800.0),  # crosses B1
        _sample(1, 70_000, 4200.0),  # crosses B2
        _sample(1, 90_000, 6000.0),
        _sample(2, 100_000, 0.0),  # closes S2 of lap 1
    ]
    records = list(compute_sector_times(samples, SPA_3SEC))
    assert [(r.lap_number, r.sector_index, r.time_ms) for r in records] == [
        (1, 0, 30_000),
        (1, 1, 40_000),
        (1, 2, 30_000),
    ]


def test_sector_times_sum_to_lap_time() -> None:
    samples = [
        _sample(1, 0, 0.0),
        _sample(1, 25_000, 1800.0),
        _sample(1, 60_000, 4200.0),
        _sample(2, 90_000, 0.0),
    ]
    records = list(compute_sector_times(samples, SPA_3SEC))
    assert sum(r.time_ms for r in records) == 90_000


def test_no_boundaries_emits_no_records() -> None:
    layout = TrackLayout(name="bare", length_m=5000.0)
    samples = [
        _sample(1, 0, 0.0),
        _sample(1, 60_000, 5000.0),
        _sample(2, 60_000, 0.0),
    ]
    assert list(compute_sector_times(samples, layout)) == []


def test_two_complete_laps_yield_six_sectors() -> None:
    samples = [
        _sample(1, 0, 0.0),
        _sample(1, 30_000, 1800.0),
        _sample(1, 70_000, 4200.0),
        _sample(2, 100_000, 0.0),
        _sample(2, 130_000, 1800.0),
        _sample(2, 170_000, 4200.0),
        _sample(3, 200_000, 0.0),
    ]
    records = list(compute_sector_times(samples, SPA_3SEC))
    assert len(records) == 6
    assert [r.lap_number for r in records] == [1, 1, 1, 2, 2, 2]
    assert [r.sector_index for r in records] == [0, 1, 2, 0, 1, 2]


def test_in_progress_final_lap_emits_no_sectors() -> None:
    # Lap 2 has no successor → its last sector (which would close on
    # lap 3's first sample) cannot be measured. Emit only lap 1.
    samples = [
        _sample(1, 0, 0.0),
        _sample(1, 30_000, 1800.0),
        _sample(1, 70_000, 4200.0),
        _sample(2, 100_000, 0.0),
        _sample(2, 130_000, 1800.0),
    ]
    records = list(compute_sector_times(samples, SPA_3SEC))
    assert [r.lap_number for r in records] == [1, 1, 1]


def test_sample_missing_lap_dist_m_does_not_reset_state() -> None:
    # A GNSS-only sample (no lap_dist_m) is informational; sector
    # tracking shouldn't crash or skip a boundary because of it.
    samples = [
        _sample(1, 0, 0.0),
        {"t_canonical_ns": 10_000_000_000, "lap_number": 1},
        _sample(1, 30_000, 1800.0),
        _sample(1, 70_000, 4200.0),
        _sample(2, 100_000, 0.0),
    ]
    records = list(compute_sector_times(samples, SPA_3SEC))
    assert [r.sector_index for r in records] == [0, 1, 2]


def test_sector_index_resets_at_lap_boundary() -> None:
    samples = [
        _sample(1, 0, 0.0),
        _sample(1, 30_000, 1800.0),
        _sample(1, 70_000, 4200.0),
        _sample(2, 100_000, 0.0),  # lap 2 first sample
        _sample(2, 130_000, 1800.0),
        _sample(2, 170_000, 4200.0),
        _sample(3, 200_000, 0.0),
    ]
    records = list(compute_sector_times(samples, SPA_3SEC))
    lap2 = [r for r in records if r.lap_number == 2]
    assert [r.sector_index for r in lap2] == [0, 1, 2]


def test_lap_with_skipped_boundary_emits_only_observed_sectors() -> None:
    # Real-world: data gap means lap 1 never reports lap_dist_m
    # crossing B2. We emit S0 (B1 crossed) but not S1 or S2.
    samples = [
        _sample(1, 0, 0.0),
        _sample(1, 30_000, 1800.0),
        _sample(1, 70_000, 4000.0),  # never reaches 4200 within lap 1
        _sample(2, 100_000, 0.0),
    ]
    records = list(compute_sector_times(samples, SPA_3SEC))
    assert [r.sector_index for r in records] == [0]


def test_returns_sector_record_dataclass_instances() -> None:
    samples = [
        _sample(1, 0, 0.0),
        _sample(1, 30_000, 1800.0),
        _sample(1, 70_000, 4200.0),
        _sample(2, 100_000, 0.0),
    ]
    [s0, _s1, _s2] = list(compute_sector_times(samples, SPA_3SEC))
    assert isinstance(s0, SectorRecord)
    assert s0.lap_number == 1
    assert s0.sector_index == 0


def test_empty_sample_stream_yields_no_records() -> None:
    assert list(compute_sector_times([], SPA_3SEC)) == []
