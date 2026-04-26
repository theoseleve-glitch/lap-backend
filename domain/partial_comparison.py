"""T049 (sprint2-backend) — partial-comparison detection.

When one or more telemetry channels are missing in either side of
a bridge pair, the score functions (T034 shape, T037 intensity)
silently skip those channels — the math is correct, but the
caller still needs to know it happened so the UI can render a
"partial comparison" badge.

A channel is excluded if EITHER side has any NaN sample. T032
fills missing values with NaN; T033 propagates any NaN to the
whole column. So the same any-NaN test that the score functions
use here is the source-of-truth signal for "channel missing".

Returns the boolean flag plus the list of excluded channel names
in canonical CHANNELS order (not input or alphabetical) so
positional consumers can rely on stable order.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from domain.channel_extractor import CHANNELS, ChannelFrame

PARTIAL_COMPARISON_LABEL = "partial-comparison"


@dataclass(frozen=True)
class PartialComparisonStatus:
    is_partial: bool
    excluded_channels: tuple[str, ...]


def detect_partial_comparison(
    a: ChannelFrame, b: ChannelFrame
) -> PartialComparisonStatus:
    excluded: list[str] = []
    for name in CHANNELS:
        ya = np.asarray(getattr(a, name), dtype=float)
        yb = np.asarray(getattr(b, name), dtype=float)
        if np.isnan(ya).any() or np.isnan(yb).any():
            excluded.append(name)
    return PartialComparisonStatus(
        is_partial=bool(excluded),
        excluded_channels=tuple(excluded),
    )
