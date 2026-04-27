"""T108 (sprint2-backend) — caution-aware strategy divergence.

Wraps T101's `compute_strategy_divergence` with FCY/SC accounting.
T097's StrategyEvent kinds include `fcy` (full course yellow) and
`sc` (safety car) — markers that the field is running at
pit-lane-equivalent pace. Bare T101 ignores them; this module
reduces effective race length by the count of caution markers
before delegating, so stint-length expectations don't penalize
strategy under caution.

Why a wrapper, not a flag on the T101 function:
  - T101's `compute_strategy_divergence` is the load-bearing
    primitive used by T102's score; changing its signature would
    cascade through callers
  - Caution accounting is a higher-level concern — a wrapping
    function that calls into T101 keeps the bare math
    composable for callers that explicitly want green-flag-only
    scoring (e.g., admin tools comparing apples-to-apples
    sessions)

Each FCY/SC marker counts as ~1 caution lap (a coarse but
defensible model — sim cautions average 1-3 laps; 1 is the
conservative case). Future refinement could derive duration
from paired fcy/sc-end events; T108 holds the surface stable.

Pit-lap delta is intentionally untouched — pit timing accuracy
is independent of cautions; smoothing it is a different,
future, more nuanced concern.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from domain.strategy_divergence import (
    StrategyDivergence,
    compute_strategy_divergence,
)


class _PlanLike(Protocol):
    planned_stints: int
    planned_fuel_liters: float
    planned_pit_laps: list[int]


class _EventLike(Protocol):
    kind: str
    at_lap: int
    details: dict[str, Any]


_CAUTION_KINDS = frozenset({"fcy", "sc"})


def count_caution_laps(events: Iterable[_EventLike]) -> int:
    """Count FCY + SC marker events. Each marker represents
    approximately one caution lap (sim cautions average 1-3
    laps; this is the conservative case)."""
    return sum(1 for e in events if e.kind in _CAUTION_KINDS)


def compute_strategy_divergence_with_cautions(
    *,
    plan: _PlanLike,
    events: Iterable[_EventLike],
    total_race_laps: int,
) -> StrategyDivergence:
    """T108 — same shape as T101's divergence, but reduces the
    effective race length by the FCY/SC caution-lap count before
    computing stint expectations.

    Effective total floors at 0 (defensive — pathological event
    logs with more caution markers than planned race length must
    not produce negative stint expectations).
    """
    events_list = list(events)
    caution_count = count_caution_laps(events_list)
    effective_total = max(0, total_race_laps - caution_count)
    return compute_strategy_divergence(
        plan=plan,
        events=events_list,
        total_race_laps=effective_total,
    )
