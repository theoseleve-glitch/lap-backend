"""T108 (sprint2-backend) — FCY/SC caution-aware strategy recalc.

T097's StrategyEvent kinds include `fcy` (full course yellow) and
`sc` (safety car). T101's plan-vs-actual divergence treats them
like any other event — `_pit_lap_delta` ignores them, `_fuel_delta`
ignores them, `_stint_duration_delta` divides by the raw
total_race_laps. That over-penalizes strategy under caution: the
field cruises at pit-lane-equivalent pace, so caution laps don't
"count" the same as green laps for stint-length expectations.

T108 introduces caution accounting:
  - `count_caution_laps(events)` — counts FCY + SC markers.
    Each marker represents approximately one caution lap (sim
    cautions average 1-3 laps in IRacing/iRacing presets;
    treating each marker as 1 lap is the conservative case).
  - `compute_strategy_divergence_with_cautions(plan, events,
    total_race_laps)` — same shape as T101's divergence, but
    reduces effective race length by the caution-lap count
    before computing stint expectations.

Pinned behaviors:
  - count_caution_laps returns 0 for an empty event log
  - count_caution_laps counts FCY events
  - count_caution_laps counts SC events
  - count_caution_laps sums FCY + SC together
  - count_caution_laps ignores other event kinds (pit, stint_end,
    plan, armed) — only caution markers reduce expectations
  - with_cautions is identical to T101 when no cautions present
    (regression pin: the recalc must not introduce drift on
    sessions that ran flat-out green)
  - with_cautions reduces effective_total_race_laps by the
    caution count before delegating to T101's math (pit_lap_delta
    is unaffected — pit timing accuracy is independent of
    cautions; only stint expectations shift)
  - effective_total floor is 0 (defensive — pathological event
    logs with more caution markers than planned race length must
    not produce negative stint expectations)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from domain.strategy_caution_recalc import (
    compute_strategy_divergence_with_cautions,
    count_caution_laps,
)
from domain.strategy_divergence import (
    StrategyDivergence,
    compute_strategy_divergence,
)


@dataclass
class _Plan:
    planned_stints: int
    planned_fuel_liters: float
    planned_pit_laps: list[int] = field(default_factory=list)


@dataclass
class _Event:
    kind: str
    at_lap: int
    details: dict[str, Any] = field(default_factory=dict)


# -- count_caution_laps ----------------------------------------


def test_count_caution_laps_empty() -> None:
    assert count_caution_laps([]) == 0


def test_count_caution_laps_counts_fcy() -> None:
    events = [
        _Event(kind="fcy", at_lap=12),
        _Event(kind="fcy", at_lap=27),
    ]
    assert count_caution_laps(events) == 2


def test_count_caution_laps_counts_sc() -> None:
    events = [
        _Event(kind="sc", at_lap=8),
        _Event(kind="sc", at_lap=20),
        _Event(kind="sc", at_lap=35),
    ]
    assert count_caution_laps(events) == 3


def test_count_caution_laps_sums_fcy_and_sc() -> None:
    events = [
        _Event(kind="fcy", at_lap=10),
        _Event(kind="sc", at_lap=22),
        _Event(kind="fcy", at_lap=40),
    ]
    assert count_caution_laps(events) == 3


def test_count_caution_laps_ignores_other_kinds() -> None:
    """Only FCY/SC markers reduce stint expectations. A pit
    stop, plan re-set, stint end, or armed alternate strategy
    is not a caution and must NOT count. Pin: a regression
    counting `pit` events would catastrophically over-shrink
    effective race length."""
    events = [
        _Event(kind="plan", at_lap=0),
        _Event(kind="armed", at_lap=5),
        _Event(kind="pit", at_lap=15),
        _Event(kind="stint_end", at_lap=15),
        _Event(kind="pit", at_lap=30),
        _Event(kind="stint_end", at_lap=30),
    ]
    assert count_caution_laps(events) == 0


def test_count_caution_laps_mixed_with_irrelevant() -> None:
    """Realistic mixed log: 5 events total, 2 cautions."""
    events = [
        _Event(kind="plan", at_lap=0),
        _Event(kind="fcy", at_lap=10),
        _Event(kind="pit", at_lap=15, details={"fuel_added_liters": 40.0}),
        _Event(kind="sc", at_lap=25),
        _Event(kind="stint_end", at_lap=30),
    ]
    assert count_caution_laps(events) == 2


# -- compute_strategy_divergence_with_cautions ----------------


def test_with_cautions_equals_t101_when_no_cautions() -> None:
    """Regression pin: when the event log has zero FCY/SC
    markers, the caution-aware function must return EXACTLY the
    same StrategyDivergence as T101's bare function. Drift here
    would silently rewrite green-flag session scoring."""
    plan = _Plan(planned_stints=2, planned_fuel_liters=80.0,
                 planned_pit_laps=[30])
    events = [
        _Event(kind="pit", at_lap=29,
               details={"fuel_added_liters": 40.0}),
        _Event(kind="stint_end", at_lap=29),
        _Event(kind="stint_end", at_lap=58),
    ]
    bare = compute_strategy_divergence(
        plan=plan, events=events, total_race_laps=60,
    )
    with_caution = compute_strategy_divergence_with_cautions(
        plan=plan, events=events, total_race_laps=60,
    )
    assert with_caution == bare


def test_with_cautions_shrinks_effective_race_length() -> None:
    """Two SC events present in the event log reduce the
    effective race length from 60 to 58 (60 - 2). With
    planned_stints=2, expected_per_stint becomes 29 instead of 30.

    Concretely:
      - actual stint lengths from stint_end events at laps 30, 58:
        first stint = 30, second stint = 58 - 30 = 28
      - bare T101 (race=60, expected_per_stint=30):
        |30 - 30| + |28 - 30| = 0 + 2 = 2
      - with cautions (effective=58, expected_per_stint=29):
        |30 - 29| + |28 - 29| = 1 + 1 = 2

    Pick numbers where the result actually differs to catch the
    drift. Use planned=3 stints over 60 race laps with 6
    cautions: bare expects 20 lap stints, with-cautions expects
    18 lap stints. With actuals at [18, 18, 18]:
      - bare: |18-20|*3 = 6
      - with: |18-18|*3 = 0
    """
    plan = _Plan(planned_stints=3, planned_fuel_liters=120.0,
                 planned_pit_laps=[18, 36, 54])
    events = [
        _Event(kind="fcy", at_lap=5),
        _Event(kind="fcy", at_lap=10),
        _Event(kind="sc", at_lap=15),
        _Event(kind="sc", at_lap=20),
        _Event(kind="sc", at_lap=25),
        _Event(kind="fcy", at_lap=30),
        # Three actual stints of 18 laps each (matching adjusted
        # expected_per_stint of 18 = 54 / 3).
        _Event(kind="stint_end", at_lap=18),
        _Event(kind="stint_end", at_lap=36),
        _Event(kind="stint_end", at_lap=54),
    ]
    bare = compute_strategy_divergence(
        plan=plan, events=events, total_race_laps=60,
    )
    with_caution = compute_strategy_divergence_with_cautions(
        plan=plan, events=events, total_race_laps=60,
    )
    # Bare T101: expected = 60 // 3 = 20, deltas = |18-20|*3 = 6.
    assert bare.stint_duration_delta_laps == 6
    # With cautions: effective = 60 - 6 = 54; expected = 18;
    # deltas = 0.
    assert with_caution.stint_duration_delta_laps == 0


def test_with_cautions_pit_lap_delta_unchanged() -> None:
    """Pit-lap accuracy is independent of cautions: the planned
    pit at lap 30 is "missed by 5 laps" whether or not yellows
    occurred. Pin: T108's recalc must NOT secondarily smooth
    pit-lap deltas — that's a different, future, more nuanced
    refinement (different value to learn from)."""
    plan = _Plan(planned_stints=2, planned_fuel_liters=80.0,
                 planned_pit_laps=[30])
    events = [
        _Event(kind="fcy", at_lap=10),
        _Event(kind="sc", at_lap=20),
        _Event(kind="pit", at_lap=35,
               details={"fuel_added_liters": 40.0}),
        _Event(kind="stint_end", at_lap=35),
    ]
    bare = compute_strategy_divergence(
        plan=plan, events=events, total_race_laps=60,
    )
    with_caution = compute_strategy_divergence_with_cautions(
        plan=plan, events=events, total_race_laps=60,
    )
    # |35 - 30| = 5 in both versions.
    assert bare.pit_lap_delta_total == 5
    assert with_caution.pit_lap_delta_total == 5


def test_with_cautions_floors_effective_total_at_zero() -> None:
    """Defensive: if a pathological event log has more caution
    markers than the planned race length, effective_total must
    floor at 0 (NOT go negative — that would propagate to
    nonsensical negative expected_per_stint and break the
    abs() math). Pin: the floor catches malformed test fixtures
    and adversarial inputs."""
    plan = _Plan(planned_stints=2, planned_fuel_liters=80.0,
                 planned_pit_laps=[30])
    events = [
        _Event(kind="fcy", at_lap=i) for i in range(70)
    ]
    result = compute_strategy_divergence_with_cautions(
        plan=plan, events=events, total_race_laps=60,
    )
    assert isinstance(result, StrategyDivergence)
    # Effective <=0 → expected_per_stint short-circuits to 0
    # via T101's defensive `if total_race_laps <= 0: return 0`.
    assert result.stint_duration_delta_laps == 0
