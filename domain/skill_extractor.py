"""T042 + T043 (sprint2-backend) — skill (per-channel) extraction.

A "skill"/"dimension" maps 1:1 to a telemetry channel
(brake, throttle, steering, speed, g_lat, g_lon).

T042 — transferred:     shape ≥0.85 AND intensity ≥0.85
T043 — non-transferred: shape <0.7 OR intensity <0.5

The two are disjoint (transferred needs both ≥0.85; non-transferred
needs one <0.7 or <0.5). A channel can fall into the moderate band
where neither fires — that's "partial transfer", not classified
here.

Channels excluded from either per-channel score (NaN-poisoned,
zero variance, both-sides-zero) appear in NEITHER list — a skill
that can't be measured isn't a transferred skill or a known
weakness.

Both outputs are in canonical CHANNELS order, not score order, so
consumers can rely on stable positional semantics.
"""

from __future__ import annotations

from domain.channel_extractor import CHANNELS, ChannelFrame
from domain.intensity_match import intensity_match_per_channel
from domain.shape_match import shape_match_per_channel

TRANSFER_THRESHOLD = 0.85
NON_TRANSFER_SHAPE_THRESHOLD = 0.7
NON_TRANSFER_INTENSITY_THRESHOLD = 0.5


def extract_transferred_skills(
    a: ChannelFrame, b: ChannelFrame
) -> list[str]:
    shape = shape_match_per_channel(a, b)
    intensity = intensity_match_per_channel(a, b)
    return [
        name
        for name in CHANNELS
        if name in shape
        and name in intensity
        and shape[name] >= TRANSFER_THRESHOLD
        and intensity[name] >= TRANSFER_THRESHOLD
    ]


def extract_non_transferred_skills(
    a: ChannelFrame, b: ChannelFrame
) -> list[str]:
    shape = shape_match_per_channel(a, b)
    intensity = intensity_match_per_channel(a, b)
    return [
        name
        for name in CHANNELS
        if name in shape
        and name in intensity
        and (
            shape[name] < NON_TRANSFER_SHAPE_THRESHOLD
            or intensity[name] < NON_TRANSFER_INTENSITY_THRESHOLD
        )
    ]
