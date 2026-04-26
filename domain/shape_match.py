"""T034 (sprint2-backend) — shape match score.

Per-channel normalized cross-correlation at lag 0 between two
ChannelFrames (typically the post-T033 normalized 500-point
output). NCC at lag 0 is Pearson r — same formula. We only care
about lag 0 because T033 already aligned both signals onto a
shared x-grid spanning each corner.

Channels with NaN (T033's column poisoning) or zero variance
(constant signal — undefined NCC, 0/0) are skipped. Negative r
is clipped to 0: anti-correlation isn't a "match" in the bridge
domain, where we're comparing two attempts at the same corner.

Returns a single float in [0, 1]. If every channel is excluded,
returns 0.0 rather than NaN so the score stays consumer-safe.
"""

from __future__ import annotations

import numpy as np

from domain.channel_extractor import CHANNELS, ChannelFrame


def shape_match_score(a: ChannelFrame, b: ChannelFrame) -> float:
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
        if ya.std() == 0.0 or yb.std() == 0.0:
            continue
        r = float(np.corrcoef(ya, yb)[0, 1])
        scores.append(max(0.0, r))

    if not scores:
        return 0.0
    return sum(scores) / len(scores)
