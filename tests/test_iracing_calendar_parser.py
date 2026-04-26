"""T086 (sprint2-backend) — iRacing calendar payload parser.

T085 built the scraper that takes a list of entry-shaped dicts
and upserts them. T086 builds the parser that converts a *raw*
iRacing API response (nested series→schedules + a separate
special_events list) into that flat entry list.

Pure-domain function — no I/O, no DB. T087's cron will call:
  raw = await fetch_raw(...)
  entries = parse_iracing_calendar_payload(raw)
  await fetch_and_store_iracing_calendar(entries=entries, ...)

Why parser separate from scraper: the iRacing API shape is
stable but verbose; the entry shape is what our domain cares
about. Decoupling means iRacing format churn touches only the
parser, and downstream consumers (T086's tests, T092 suggestion
algorithm) use the stable internal shape.

Acceptance:
  - parse a series with N schedule weeks → N entries with
    kind="series_week", source_id derived as
    "ir-series-<series_id>-week-<race_week_num>"
  - parse special_events → entries with kind="special_event",
    source_id "ir-special-<event_id>"
  - first car_class on the series propagates to every week
    (one car class per series — iRacing's data shape choice)
  - track_name is "<track_name> <config_name>" when config is
    present, just <track_name> when it isn't (catches both
    Spa Francorchamps GP and a stand-alone street circuit)
  - missing/empty special_events list still parses cleanly
  - missing/empty series list still parses cleanly
  - source_ids are unique within one parse output (no collision
    between two weeks of the same series, or two specials)
  - source_id is deterministic across re-parses of the same
    payload — T085's upsert depends on this stability
"""

from __future__ import annotations

from typing import Any

from infra.iracing_calendar import parse_iracing_calendar_payload


def _raw_payload() -> dict[str, Any]:
    """Synthetic payload mirroring the iRacing data API shape:
    `series` list with nested `schedules`, plus a separate
    `special_events` list."""
    return {
        "series": [
            {
                "series_id": 12345,
                "series_name": "Porsche Cup 2026 Season 2",
                "car_classes": [{"name": "Porsche 911 Cup"}],
                "schedules": [
                    {
                        "race_week_num": 5,
                        "track": {
                            "track_id": 200,
                            "track_name": "Spa-Francorchamps",
                            "config_name": "GP",
                        },
                        "start_date": "2026-05-04T00:00:00+00:00",
                        "end_date": "2026-05-10T23:59:59+00:00",
                    },
                    {
                        "race_week_num": 6,
                        "track": {
                            "track_id": 201,
                            "track_name": "Long Beach Street Circuit",
                            "config_name": None,
                        },
                        "start_date": "2026-05-11T00:00:00+00:00",
                        "end_date": "2026-05-17T23:59:59+00:00",
                    },
                ],
            }
        ],
        "special_events": [
            {
                "event_id": 9999,
                "event_name": "Daytona 24 Hours 2026",
                "track": {
                    "track_id": 300,
                    "track_name": "Daytona International Speedway",
                    "config_name": "24h",
                },
                "start_at": "2026-12-12T18:00:00+00:00",
                "end_at": "2026-12-13T18:00:00+00:00",
            }
        ],
    }


# -- series weeks --------------------------------------------------


def test_series_weeks_become_entries_with_stable_source_ids() -> None:
    """Two weeks of one series → 2 entries, kind=series_week,
    source_id encodes both series and week. Pin: T085's upsert
    requires source_id to be stable across re-parses."""
    entries = parse_iracing_calendar_payload(_raw_payload())
    weeks = [e for e in entries if e["kind"] == "series_week"]
    assert len(weeks) == 2
    week_ids = {e["source_id"] for e in weeks}
    assert week_ids == {
        "ir-series-12345-week-5",
        "ir-series-12345-week-6",
    }


def test_series_week_propagates_car_class_to_every_week() -> None:
    """car_classes lives on the series, not on individual schedule
    rows in iRacing's API. The parser must propagate it down so
    each entry knows its car class. Pin: missing this = entries
    have no car_class info, breaks T092 suggestion algorithm
    that filters by car class."""
    entries = parse_iracing_calendar_payload(_raw_payload())
    weeks = [e for e in entries if e["kind"] == "series_week"]
    assert all(e["car_class"] == "Porsche 911 Cup" for e in weeks)


def test_track_name_includes_config_when_present() -> None:
    """Spa GP layout vs Long Beach (no config). When config_name
    is set, track_name is "<track> <config>" so consumers can
    distinguish Spa GP from Spa endurance. Without config,
    track_name is the raw track name. Pin: a regression that
    always includes config would emit "Long Beach Street Circuit
    None"."""
    entries = parse_iracing_calendar_payload(_raw_payload())
    by_id = {e["source_id"]: e for e in entries}
    assert (
        by_id["ir-series-12345-week-5"]["track_name"]
        == "Spa-Francorchamps GP"
    )
    assert (
        by_id["ir-series-12345-week-6"]["track_name"]
        == "Long Beach Street Circuit"
    )


def test_series_week_starts_and_ends_round_trip() -> None:
    """ISO-8601 strings preserved through the parser into the
    entry dicts (the scraper does the datetime.fromisoformat
    parse). Pin: a regression that drops timezone or strips the
    'T' would break the scraper's parse step."""
    entries = parse_iracing_calendar_payload(_raw_payload())
    by_id = {e["source_id"]: e for e in entries}
    week5 = by_id["ir-series-12345-week-5"]
    assert week5["starts_at"] == "2026-05-04T00:00:00+00:00"
    assert week5["ends_at"] == "2026-05-10T23:59:59+00:00"


# -- special events ------------------------------------------------


def test_special_events_become_entries_with_special_kind() -> None:
    """Daytona 24 → one entry with kind=special_event, source_id
    "ir-special-<event_id>". Pin: distinct kind so T092 can
    weight specials differently from rotating series weeks."""
    entries = parse_iracing_calendar_payload(_raw_payload())
    specials = [e for e in entries if e["kind"] == "special_event"]
    assert len(specials) == 1
    assert specials[0]["source_id"] == "ir-special-9999"
    assert specials[0]["event_name"] == "Daytona 24 Hours 2026"


def test_special_event_has_no_car_class() -> None:
    """Special events run multiple car classes (Daytona 24 has
    GTP + GTD + GT3 etc). The parser leaves car_class as None
    rather than picking one — picking would silently mis-tag.
    Pin: T092's matching algorithm relies on None to mean
    "open to any car class"."""
    entries = parse_iracing_calendar_payload(_raw_payload())
    specials = [e for e in entries if e["kind"] == "special_event"]
    assert specials[0]["car_class"] is None


# -- empty inputs --------------------------------------------------


def test_empty_payload_returns_empty_entries() -> None:
    """Both arrays empty → empty entry list, no crash. Pin: a
    real iRacing fetch off-season may legitimately return both
    empty; the cron must not error."""
    assert parse_iracing_calendar_payload(
        {"series": [], "special_events": []}
    ) == []


def test_missing_special_events_key_treated_as_empty() -> None:
    """iRacing's API may omit the special_events key entirely if
    none are scheduled. Pin: defensive parsing — missing key !=
    crash. A regression to dict[key] would KeyError here."""
    raw = {"series": []}
    assert parse_iracing_calendar_payload(raw) == []


def test_missing_series_key_treated_as_empty() -> None:
    """Symmetric to special_events missing. Pin: defensive
    parsing of both top-level arrays."""
    raw = {"special_events": []}
    assert parse_iracing_calendar_payload(raw) == []


# -- uniqueness pin -----------------------------------------------


def test_all_source_ids_in_output_are_unique() -> None:
    """No two entries share a source_id within one parse —
    otherwise T085's upsert would silently overwrite the
    second with the first within the same run. Pin: derivation
    must include enough discriminators (series_id + week_num
    for series, event_id for specials)."""
    entries = parse_iracing_calendar_payload(_raw_payload())
    ids = [e["source_id"] for e in entries]
    assert len(ids) == len(set(ids))


def test_source_ids_are_deterministic_across_reparses() -> None:
    """Parsing the same payload twice yields the same source_ids
    in the same order. Pin: T085's upsert keys on source_id, so
    parser nondeterminism (e.g., adding a uuid) would create
    duplicate rows on every cron fire."""
    payload = _raw_payload()
    first = [e["source_id"] for e in parse_iracing_calendar_payload(payload)]
    second = [e["source_id"] for e in parse_iracing_calendar_payload(payload)]
    assert first == second
