"""Canonical timestamp alignment (T121, ADR-007).

Per ADR-007, every captured sample carries `t_source_ns` from the
sensor's own clock and `t_canonical_ns` in the session's canonical
timebase. Capture clients ship raw `t_source_ns`; alignment runs
server-side here.

Rules:
    - Canonical clocks (irsdk_tick, acc_shmem, gnss, racebox_gnss)
      ARE canonical for their session. t_canonical_ns = t_source_ns.
    - Derivative clocks (coremotion, device_monotonic) align to
      bracketing canonical anchors via linear interpolation. Values
      outside the anchor range clamp to the nearest anchor.
    - When no canonical anchors exist (single-clock stream), the
      lone clock IS canonical by elimination.
    - Samples already carrying t_canonical_ns pass through.
    - Samples without t_source_ns can't be aligned and pass through
      without t_canonical_ns rather than being silently dropped.
"""

from collections.abc import Iterable
from typing import Any

CANONICAL_CLOCKS = frozenset(
    {"irsdk_tick", "acc_shmem", "gnss", "racebox_gnss"}
)


def align_canonical_timestamps(
    samples: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    materialized = [dict(s) for s in samples]

    anchors: list[tuple[int, int]] = []
    for s in materialized:
        if s.get("t_source_clock") not in CANONICAL_CLOCKS:
            continue
        t_src = s.get("t_source_ns")
        if not isinstance(t_src, int):
            continue
        t_can = s.get("t_canonical_ns")
        anchors.append((t_src, t_can if isinstance(t_can, int) else t_src))
    anchors.sort(key=lambda pair: pair[0])

    for s in materialized:
        if isinstance(s.get("t_canonical_ns"), int):
            continue
        t_src = s.get("t_source_ns")
        if not isinstance(t_src, int):
            continue
        clock = s.get("t_source_clock")
        if clock in CANONICAL_CLOCKS or not anchors:
            s["t_canonical_ns"] = t_src
        else:
            s["t_canonical_ns"] = _interpolate(t_src, anchors)

    return materialized


def _interpolate(t_source: int, anchors: list[tuple[int, int]]) -> int:
    if t_source <= anchors[0][0]:
        return anchors[0][1]
    if t_source >= anchors[-1][0]:
        return anchors[-1][1]
    for (a_src, a_can), (b_src, b_can) in zip(
        anchors, anchors[1:], strict=False
    ):
        if a_src <= t_source <= b_src:
            if b_src == a_src:
                return a_can
            ratio = (t_source - a_src) / (b_src - a_src)
            return a_can + round(ratio * (b_can - a_can))
    return anchors[-1][1]
