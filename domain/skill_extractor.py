"""T042 (sprint2-backend) — transferred-skills extraction.

A "skill"/"dimension" maps 1:1 to a telemetry channel
(brake, throttle, steering, speed, g_lat, g_lon). A skill is
*transferred* when both per-channel metrics cross the 0.85 floor.

Channels excluded from either per-channel score (NaN-poisoned,
zero variance, both-sides-zero) are never transferred — a skill
that can't be measured can't be claimed transferred.

Output is in canonical CHANNELS order, not score order, so
consumers can rely on stable positional semantics.
"""

from __future__ import annotations

from domain.channel_extractor import CHANNELS, ChannelFrame
from domain.intensity_match import intensity_match_per_channel
from domain.shape_match import shape_match_per_channel

TRANSFER_THRESHOLD = 0.85


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
