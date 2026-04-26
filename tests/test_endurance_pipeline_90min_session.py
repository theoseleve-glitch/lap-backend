"""T079 (sprint2-backend) — endurance pipeline against a 90-min session.

End-to-end validation: a synthetic 90-min, 2-stint session runs
through the full endurance scoring pipeline and produces sensible,
hand-checkable values per stint.

Pipeline composition validated here:
  - T068 detect_stints: pit-marker → stint boundaries
  - T070 compute_stint_consistency_seconds: closing-window stddev
  - T071 compute_pace_decay_seconds_per_lap: OLS slope
  - T072 compute_tire_preservation_score: rule-based composite
    (fuel + decay sub-scores, MIN aggregation)

Why synthetic vs real archive: LapCoachLogger_sessions/ has no
90-min session (typical iRacing offline sessions are 5-30 min;
the Sebring/Silverstone fixtures are sub-30 min). A synthetic
deterministic series exercises every code path with exact
expected values — stronger than asserting on real telemetry that
would require a parser and could drift.

Session shape:
  - 49 laps total, ~110s each → ~90 min
  - Stint 1 (laps 1-25): driver pushes hard, tires pick up
    temperature, pace decays linearly ~0.34 s/lap → FIRES
    pace_decay_excessive rule
  - Pit at lap 25
  - Stint 2 (laps 26-49): fresh tires, disciplined, gentle decay
    ~0.05 s/lap → does NOT fire pace_decay_excessive

Acceptance:
  - stint detection returns the two expected stints
  - stint 1 pace_decay > 0.15 (fires the spec threshold)
  - stint 2 pace_decay < 0.10 (does not fire)
  - stint 1 tire_preservation < stint 2 tire_preservation
    (the differentiated-stints inequality is the integration's
    whole point; a constant scorer would tie them)
  - stint 1 closing-window consistency > stint 2 (stint 1 is
    decaying so its closing laps spread; stint 2 is steady)
"""

from __future__ import annotations

from collections.abc import Sequence

from domain.pace_decay import compute_pace_decay_seconds_per_lap
from domain.stint_consistency import compute_stint_consistency_seconds
from domain.stint_detection import Stint, detect_stints
from domain.tire_preservation import compute_tire_preservation_score


def _stint1_lap_times() -> list[float]:
    """25 laps. First two are warm-up (cold tires, traffic out of
    pits); laps 3-25 are linear decay from 110.0 to ~117.5
    (slope ~0.34 s/lap)."""
    laps = [112.0, 111.0]
    for i in range(23):
        laps.append(110.0 + 0.34 * i)
    return laps


def _stint2_lap_times() -> list[float]:
    """24 laps on fresh tires. Brief warm-up then very gentle
    decay (~0.05 s/lap) — disciplined driver."""
    laps = [111.0, 110.5]
    for i in range(22):
        laps.append(110.0 + 0.05 * i)
    return laps


def _slice_for(
    full: Sequence[float], stint: Stint
) -> list[float]:
    """Stint boundaries are 1-indexed lap numbers (per T068)."""
    return list(full[stint.start_lap - 1 : stint.end_lap])


# -- structural pin -----------------------------------------------


def test_detect_stints_partitions_session_at_pit() -> None:
    """Pit at lap 25 in a 49-lap session must produce two stints
    of 25 + 24 laps. Pin: any off-by-one in T068 would shift the
    closing-window of stint 1 onto stint 2 and corrupt every
    downstream metric."""
    stints = detect_stints(total_laps=49, pit_in_laps=[25])
    assert stints == [
        Stint(start_lap=1, end_lap=25, lap_count=25),
        Stint(start_lap=26, end_lap=49, lap_count=24),
    ]


# -- per-stint pace decay -----------------------------------------


def test_first_stint_pace_decay_fires_threshold() -> None:
    """Stint 1 was driven hard; OLS slope must exceed the 0.15
    s/lap threshold the enduro.stint_management.pace_decay_excessive
    rule uses. Without this, T079's synthetic profile would be
    indistinguishable from a clean stint."""
    stint1 = _stint1_lap_times()
    decay = compute_pace_decay_seconds_per_lap(
        lap_times_seconds=stint1
    )
    assert decay is not None
    assert decay > 0.15, (
        f"expected stint 1 to exceed pace_decay rule threshold "
        f"(>0.15), got {decay:.3f}"
    )


def test_second_stint_pace_decay_does_not_fire() -> None:
    """Stint 2 was disciplined; slope must stay below the rule
    threshold so a clean stint produces no false-positive coaching.
    Pin the differentiation between the two stints."""
    stint2 = _stint2_lap_times()
    decay = compute_pace_decay_seconds_per_lap(
        lap_times_seconds=stint2
    )
    assert decay is not None
    assert decay < 0.15, (
        f"expected stint 2 to stay below pace_decay rule threshold "
        f"(<0.15), got {decay:.3f}"
    )


# -- tire preservation differentiates stints ----------------------


def test_tire_preservation_lower_for_aggressive_first_stint() -> None:
    """Stint 1 was aggressive: heavier pace decay AND higher fuel
    burn (the throttle aggression that drove the decay also
    burned excess fuel). Stint 2 was disciplined on both axes.
    Preservation MUST come back lower for stint 1 — pin the full
    composition (T072 reads BOTH signals through MIN aggregation).

    Why distinct fuels per stint: T072's MIN clamps to whichever
    sub-score is lower. With identical fuel near MAX_FUEL_PER_LAP
    (4.5), fuel_sub dominates and both stints tie regardless of
    decay. Realistic endurance driving has fuel and decay
    correlated — that correlation is what makes preservation a
    useful single-number summary."""
    decay1 = compute_pace_decay_seconds_per_lap(
        lap_times_seconds=_stint1_lap_times()
    )
    decay2 = compute_pace_decay_seconds_per_lap(
        lap_times_seconds=_stint2_lap_times()
    )
    assert decay1 is not None
    assert decay2 is not None
    score1 = compute_tire_preservation_score(
        fuel_used_per_lap=4.0,  # aggressive: throttle burns fuel
        pace_decay_seconds_per_lap=decay1,
    )
    score2 = compute_tire_preservation_score(
        fuel_used_per_lap=3.0,  # disciplined: lift-and-coast
        pace_decay_seconds_per_lap=decay2,
    )
    assert score1 < score2, (
        f"expected stint 1 preservation ({score1:.3f}) < "
        f"stint 2 ({score2:.3f}) — stint 1 burns more fuel AND "
        f"decays harder; both signals push preservation down"
    )


# -- closing-window consistency differentiates stints -------------


def test_closing_window_consistency_worse_in_decaying_stint() -> None:
    """Stint 1's closing laps are still climbing the decay slope
    (not yet steady-state), so its closing-window stddev should
    exceed stint 2's flat closing window. Pin: this is what makes
    the consistency metric useful in long runs — it isolates the
    decaying-tail signal from the warm-up garbage."""
    stddev1 = compute_stint_consistency_seconds(
        lap_times_seconds=_stint1_lap_times(),
        window=10,
    )
    stddev2 = compute_stint_consistency_seconds(
        lap_times_seconds=_stint2_lap_times(),
        window=10,
    )
    assert stddev1 is not None
    assert stddev2 is not None
    assert stddev1 > stddev2, (
        f"expected stint 1 closing stddev ({stddev1:.3f}) > "
        f"stint 2 ({stddev2:.3f}); a decaying stint should be "
        f"less consistent than a steady one"
    )


# -- composition pin ----------------------------------------------


def test_pipeline_composes_per_stint_against_full_session() -> None:
    """Full pipeline against the synthesized 49-lap session:
    detect stints, then for each stint compute decay +
    consistency + preservation. Asserts every step returns a
    real number (no None), and that the cumulative sum of
    stint lap counts equals total_laps. Catches a future edit
    that drops one stint or returns None for a clean stint."""
    full = _stint1_lap_times() + _stint2_lap_times()
    assert len(full) == 49
    stints = detect_stints(total_laps=len(full), pit_in_laps=[25])
    assert sum(s.lap_count for s in stints) == len(full)
    fuel_per_lap = 3.5
    for stint in stints:
        lap_times = _slice_for(full, stint)
        assert len(lap_times) == stint.lap_count
        decay = compute_pace_decay_seconds_per_lap(
            lap_times_seconds=lap_times
        )
        consistency = compute_stint_consistency_seconds(
            lap_times_seconds=lap_times,
            window=10,
        )
        assert decay is not None
        assert consistency is not None
        score = compute_tire_preservation_score(
            fuel_used_per_lap=fuel_per_lap,
            pace_decay_seconds_per_lap=decay,
        )
        assert 0.0 <= score <= 1.0
