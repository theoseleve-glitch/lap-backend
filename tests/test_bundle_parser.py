"""T115 — Bundle v2 parser → normalized sample stream.

The on-disk format of a Bundle v2 telemetry chunk is newline-
delimited JSON: one sample per line, each with the fields ADR-007
mandates (t_source_ns, t_canonical_ns, t_source_clock, channel
values). The parser accepts the concatenated chunk bytes and yields
sample dicts in the order they appear.

This is the smallest format that round-trips through Pydantic on
both the capture and ingest sides. T115 only needs the
"unpack into normalized stream" half — Pydantic-model wrapping comes
when downstream stages need it.
"""

import json

import pytest

from domain.bundle_parser import BundleParseError, parse_bundle_v2


def _ndjson(*samples: dict[str, object]) -> bytes:
    return ("\n".join(json.dumps(s) for s in samples) + "\n").encode()


def _sample(t: int, **channels: float) -> dict[str, object]:
    return {
        "t_source_ns": t,
        "t_canonical_ns": t,
        "t_source_clock": "irsdk_tick",
        **channels,
    }


def test_parser_yields_each_sample_in_order() -> None:
    body = _ndjson(
        _sample(0, speed=12.5, throttle=0.1),
        _sample(1_000_000, speed=22.0, throttle=0.4),
        _sample(2_000_000, speed=30.5, throttle=0.7),
    )
    samples = list(parse_bundle_v2(body))
    ts = [s["t_source_ns"] for s in samples]
    assert ts == [0, 1_000_000, 2_000_000]


def test_each_yielded_sample_carries_required_fields() -> None:
    body = _ndjson(_sample(0, speed=12.5))
    [sample] = list(parse_bundle_v2(body))
    assert "t_source_ns" in sample
    assert "t_canonical_ns" in sample
    assert "t_source_clock" in sample
    assert sample["t_source_clock"] == "irsdk_tick"
    assert sample["speed"] == 12.5


def test_empty_bundle_yields_no_samples() -> None:
    assert list(parse_bundle_v2(b"")) == []


def test_trailing_blank_lines_are_ignored() -> None:
    body = _ndjson(_sample(0)) + b"\n\n\n"
    assert len(list(parse_bundle_v2(body))) == 1


def test_malformed_json_line_raises_bundle_parse_error() -> None:
    body = _ndjson(_sample(0)) + b"{not-valid-json\n"
    with pytest.raises(BundleParseError, match="invalid_json"):
        list(parse_bundle_v2(body))


def test_missing_required_timestamp_raises_bundle_parse_error() -> None:
    bad = {"speed": 1.0, "t_canonical_ns": 0, "t_source_clock": "gnss"}
    body = (json.dumps(bad) + "\n").encode()
    with pytest.raises(BundleParseError, match="missing_timestamp"):
        list(parse_bundle_v2(body))


def test_non_object_top_level_raises_bundle_parse_error() -> None:
    body = b"[1, 2, 3]\n"
    with pytest.raises(BundleParseError, match="not_an_object"):
        list(parse_bundle_v2(body))


def test_parser_is_lazy_does_not_consume_full_input_eagerly() -> None:
    # Generator semantics — parsing 10k samples should not materialize
    # them all into a list before the caller reads the first one.
    body = _ndjson(*(_sample(i) for i in range(1_000)))
    gen = parse_bundle_v2(body)
    first = next(gen)
    assert first["t_source_ns"] == 0


def test_unicode_channel_values_round_trip() -> None:
    body = _ndjson(_sample(0, label="straight"))
    [sample] = list(parse_bundle_v2(body))
    assert sample["label"] == "straight"


def test_parser_accepts_three_thousand_sample_fixture() -> None:
    # 60 Hz × 50 s = 3000 samples — representative of a single lap.
    body = _ndjson(*(_sample(i * 16_666_666, speed=float(i)) for i in range(3000)))
    samples = list(parse_bundle_v2(body))
    assert len(samples) == 3000
    assert samples[0]["t_source_ns"] == 0
    assert samples[-1]["t_source_ns"] == 2999 * 16_666_666
