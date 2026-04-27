"""T103 (sprint2-backend) — backend-side contract test for the
SessionManifest.strategy_section bundle extension.

The schema and round-trip itself live in lap-schemas. This test
pins the cross-repo wiring from the backend's perspective: it
proves that lap-backend's editable lap-schemas dependency
exposes the new optional field, accepts a manifest dict carrying
it, and round-trips the section through Pydantic the same way
it would when the ingest router calls SessionManifest.model_validate.

Why a backend test for a schema field? If lap-backend ever drifts
to a stale pinned lap-schemas, capture clients sending v2 bundles
with strategy_section would silently fail validation at the
ingest boundary. This test catches that drift before runtime.

Two pins:
1. omitting strategy_section keeps every existing manifest valid
   (additive non-breaking) — no migration of capture clients.
2. providing strategy_section round-trips planned_stints,
   planned_fuel_liters, and planned_pit_laps unchanged — the
   bundle's pre-race intent survives the boundary intact.
"""

from __future__ import annotations

import json
from typing import Any

import uuid6
from lap_schemas.v1.session import SessionManifest
from lap_schemas.v1.session_strategy_section import SessionStrategySection


def _base_manifest_dict() -> dict[str, Any]:
    """Smallest valid SessionManifest payload — no strategy_section."""
    return {
        "session_id": str(uuid6.uuid7()),
        "driver_id": str(uuid6.uuid7()),
        "bundle_version": "v2",
        "capture_app": "lap-coach-logger",
        "capture_app_version": "0.9.0",
        "device_model": "Desktop",
        "device_os": "Linux",
        "device_os_version": "6.1",
        "source": "iracing",
        "sim_title": "iracing",
        "real_hardware": None,
        "car_id": str(uuid6.uuid7()),
        "track_id": str(uuid6.uuid7()),
        "track_layout": "gp",
        "environment_tag": "sim_race",
        "objective_tag": "race_prep",
        "training_block_id": None,
        "started_at": "2026-04-24T18:00:00Z",
        "ended_at": "2026-04-24T22:00:00Z",
        "duration_seconds": 14400.0,
        "telemetry_checksums": {"samples.bin": "sha256:xyz"},
        "total_samples": 864000,
        "sample_rate_hz": 60.0,
        "data_quality": {
            "score": 95.0,
            "channel_completeness": 100.0,
            "sample_gaps_count": 0,
            "sample_rate_stability": 0.0,
            "usable_for_overlays": True,
            "usable_for_long_term_coaching": True,
        },
        "sensor_config": {"sensors": []},
    }


def test_manifest_validates_without_strategy_section() -> None:
    """Existing v2 capture clients (sprint sessions, free practice)
    that don't carry a pre-race plan must keep validating. Pin: a
    backend pinned to a pre-T103 lap-schemas would still pass this
    too — but it's the regression baseline for the second test."""
    parsed = SessionManifest.model_validate(_base_manifest_dict())
    assert parsed.strategy_section is None


def test_manifest_validates_with_strategy_section_round_trip() -> None:
    """A v2 manifest WITH strategy_section validates and round-trips
    through model_dump_json → model_validate_json with all three
    fields preserved. Pin: if lap-backend's lap-schemas pin drifts
    to pre-T103, the field would be rejected by extra="forbid" —
    this test would fail with a ValidationError, surfacing the
    cross-repo drift before it bites at ingest time."""
    payload = _base_manifest_dict()
    payload["strategy_section"] = {
        "planned_stints": 4,
        "planned_fuel_liters": 105.0,
        "planned_pit_laps": [22, 44, 66],
    }
    parsed = SessionManifest.model_validate(payload)
    assert parsed.strategy_section == SessionStrategySection(
        planned_stints=4,
        planned_fuel_liters=105.0,
        planned_pit_laps=[22, 44, 66],
    )
    encoded = parsed.model_dump_json()
    reloaded = SessionManifest.model_validate_json(encoded)
    assert reloaded.strategy_section is not None
    assert reloaded.strategy_section.planned_stints == 4
    assert reloaded.strategy_section.planned_fuel_liters == 105.0
    assert reloaded.strategy_section.planned_pit_laps == [22, 44, 66]
    assert reloaded == parsed
    # Defense-in-depth: the JSON wire form must include the section
    # under its declared key so ingest sees it on the inbound dict.
    encoded_dict = json.loads(encoded)
    assert "strategy_section" in encoded_dict
    assert encoded_dict["strategy_section"]["planned_pit_laps"] == [22, 44, 66]
