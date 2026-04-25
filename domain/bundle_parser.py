"""Bundle v2 telemetry parser (T115).

Bundle v2 chunks are newline-delimited JSON: one sample per line,
each carrying `t_source_ns`, `t_canonical_ns`, `t_source_clock` (per
ADR-007) plus the channel values. The parser yields sample dicts
lazily — the ingest pipeline can stream-fold over millions of
samples without materializing them into a list.
"""

import json
from collections.abc import Iterator
from typing import Any

REQUIRED_TIMESTAMP_FIELDS = ("t_source_ns", "t_canonical_ns")


class BundleParseError(ValueError):
    """A Bundle v2 chunk failed to parse."""


def parse_bundle_v2(body: bytes) -> Iterator[dict[str, Any]]:
    """Yield sample dicts from a Bundle v2 chunk in order.

    Format: one JSON object per line. Blank lines are ignored. Any
    malformed line aborts the iteration with `BundleParseError`.
    """
    text = body.decode("utf-8")
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            sample = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BundleParseError(
                f"invalid_json at line {line_number}"
            ) from exc
        if not isinstance(sample, dict):
            raise BundleParseError(
                f"not_an_object at line {line_number}"
            )
        for field in REQUIRED_TIMESTAMP_FIELDS:
            if field not in sample:
                raise BundleParseError(
                    f"missing_timestamp '{field}' at line {line_number}"
                )
        yield sample
