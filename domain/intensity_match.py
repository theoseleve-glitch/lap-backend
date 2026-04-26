"""T037 (sprint2-backend) — intensity match score.

Per-channel magnitude similarity between two ChannelFrames.

Formula (per channel):
  peak_ratio  = min(|peak_a|, |peak_b|)  / max(|peak_a|, |peak_b|)
  integ_ratio = min(sum|a|,    sum|b|)   / max(sum|a|,    sum|b|)
  channel_score = mean(peak_ratio, integ_ratio)

Frame score = mean(channel_score across valid channels).

Sign-agnostic by design: shape_match handles direction; this
function answers "how hard". On the post-T033 grid both sides
share the same dx, so it cancels out of the integral ratio — we
just sum |samples|.

Skipped channels (don't count toward the average):
- any-NaN columns (T033 normalizer poisoning)
- channels where BOTH sides are entirely zero (no signal to
  compare); asymmetric zero (one side flat, one side moving) is
  scored as 0.0, not skipped — that's a meaningful gap.

Returns 0.0 (never NaN) when every channel is excluded.
"""

from __future__ import annotations

import numpy as np

from domain.channel_extractor import CHANNELS, ChannelFrame


def intensity_match_score(a: ChannelFrame, b: ChannelFrame) -> float:
    if len(a) != len(b):
        raise ValueError(
            f"frame lengths differ: {len(a)} vs {len(b)}"
        )

    scores: list[float] = []
    for name in CHANNELS:
        ya = np.asarray(getattr(a, name), dtype=float)
        yb = np.asarray(getattr(b, name), dtype=float)
        if np.isnan(ya).any() or np.isnan(yb).any():
            continue
        abs_a = np.abs(ya)
        abs_b = np.abs(yb)
        peak_a = float(abs_a.max())
        peak_b = float(abs_b.max())
        if peak_a == 0.0 and peak_b == 0.0:
            # No signal in either side — skip rather than count as
            # a "perfect" match.
            continue
        peak_ratio = min(peak_a, peak_b) / max(peak_a, peak_b)
        integ_a = float(abs_a.sum())
        integ_b = float(abs_b.sum())
        # If both peaks are non-zero, both integrals are too —
        # min/max is well-defined. If one peak is non-zero and the
        # other is zero, integ on that side is zero too → ratio 0.
        integ_ratio = (
            min(integ_a, integ_b) / max(integ_a, integ_b)
            if max(integ_a, integ_b) > 0.0
            else 0.0
        )
        scores.append((peak_ratio + integ_ratio) / 2.0)

    if not scores:
        return 0.0
    return sum(scores) / len(scores)
