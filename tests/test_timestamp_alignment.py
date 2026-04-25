"""T121 — Canonical timestamp alignment per ADR-007.

Every output sample carries `t_canonical_ns`. Samples on a
canonical clock (irsdk_tick, acc_shmem, gnss, racebox_gnss) get
t_canonical_ns = t_source_ns. Samples on a derivative clock
(coremotion, device_monotonic) are aligned by linear interpolation
between the bracketing canonical anchors. A stream that contains
only a derivative clock falls back to "source IS canonical" by
elimination — there's no canonical clock to align against.
"""

from domain.timestamp_alignment import align_canonical_timestamps


def test_existing_t_canonical_ns_is_preserved() -> None:
    samples = [
        {
            "t_source_ns": 100,
            "t_canonical_ns": 999,
            "t_source_clock": "irsdk_tick",
        },
    ]
    [out] = align_canonical_timestamps(samples)
    assert out["t_canonical_ns"] == 999


def test_irsdk_tick_uses_source_as_canonical() -> None:
    samples = [{"t_source_ns": 42, "t_source_clock": "irsdk_tick"}]
    [out] = align_canonical_timestamps(samples)
    assert out["t_canonical_ns"] == 42


def test_acc_shmem_uses_source_as_canonical() -> None:
    samples = [{"t_source_ns": 42, "t_source_clock": "acc_shmem"}]
    [out] = align_canonical_timestamps(samples)
    assert out["t_canonical_ns"] == 42


def test_gnss_uses_source_as_canonical() -> None:
    samples = [{"t_source_ns": 42, "t_source_clock": "gnss"}]
    [out] = align_canonical_timestamps(samples)
    assert out["t_canonical_ns"] == 42


def test_racebox_gnss_uses_source_as_canonical() -> None:
    samples = [{"t_source_ns": 42, "t_source_clock": "racebox_gnss"}]
    [out] = align_canonical_timestamps(samples)
    assert out["t_canonical_ns"] == 42


def test_coremotion_in_pure_stream_uses_source_as_canonical() -> None:
    # No canonical anchors anywhere → CoreMotion's clock IS canonical
    # by elimination.
    samples = [
        {"t_source_ns": 100, "t_source_clock": "coremotion"},
        {"t_source_ns": 200, "t_source_clock": "coremotion"},
    ]
    aligned = align_canonical_timestamps(samples)
    assert [s["t_canonical_ns"] for s in aligned] == [100, 200]


def test_coremotion_aligned_via_linear_interpolation() -> None:
    # Anchors carry an explicit t_canonical_ns offset so we observe
    # interpolation actually doing work, not degenerating to identity.
    samples = [
        {
            "t_source_ns": 100,
            "t_canonical_ns": 110,
            "t_source_clock": "racebox_gnss",
        },
        {"t_source_ns": 150, "t_source_clock": "coremotion"},
        {
            "t_source_ns": 200,
            "t_canonical_ns": 220,
            "t_source_clock": "racebox_gnss",
        },
    ]
    aligned = align_canonical_timestamps(samples)
    # Halfway between the anchors → 110 + 0.5*(220-110) = 165.
    assert aligned[1]["t_canonical_ns"] == 165


def test_coremotion_before_first_anchor_clamps_to_first_canonical() -> None:
    samples = [
        {"t_source_ns": 50, "t_source_clock": "coremotion"},
        {
            "t_source_ns": 100,
            "t_canonical_ns": 110,
            "t_source_clock": "racebox_gnss",
        },
    ]
    aligned = align_canonical_timestamps(samples)
    assert aligned[0]["t_canonical_ns"] == 110


def test_coremotion_after_last_anchor_clamps_to_last_canonical() -> None:
    samples = [
        {
            "t_source_ns": 100,
            "t_canonical_ns": 110,
            "t_source_clock": "racebox_gnss",
        },
        {"t_source_ns": 250, "t_source_clock": "coremotion"},
    ]
    aligned = align_canonical_timestamps(samples)
    assert aligned[1]["t_canonical_ns"] == 110


def test_other_fields_are_preserved_unchanged() -> None:
    samples = [
        {
            "t_source_ns": 42,
            "t_source_clock": "irsdk_tick",
            "speed_mps": 50.0,
            "lap_number": 3,
        },
    ]
    [out] = align_canonical_timestamps(samples)
    assert out["speed_mps"] == 50.0
    assert out["lap_number"] == 3
    assert out["t_canonical_ns"] == 42


def test_sample_without_t_source_ns_is_passed_through_without_canonical() -> None:
    # Defensive: a malformed sample doesn't crash the aligner, and
    # we don't fabricate a canonical timestamp out of nothing.
    samples = [{"t_source_clock": "coremotion"}]
    [out] = align_canonical_timestamps(samples)
    assert "t_canonical_ns" not in out


def test_does_not_mutate_input_samples() -> None:
    sample_in: dict[str, object] = {
        "t_source_ns": 42,
        "t_source_clock": "irsdk_tick",
    }
    align_canonical_timestamps([sample_in])
    assert "t_canonical_ns" not in sample_in
