"""Session processing pipeline (T124).

Orchestrates the domain pieces that produce structured outputs from
a raw session bundle: parse → align timestamps → detect laps →
metrics + data-quality. Persists `Lap` rows and a rich
`SessionRecord` payload, then transitions the session to "ready".

Kept separate from `infra/celery_app.py` so the same pipeline can
be invoked by a backfill CLI (or future job runner) without
dragging Celery imports along.
"""

import os
from typing import Any
from uuid import UUID

from sqlalchemy import select

from domain.bundle_parser import parse_bundle_v2
from domain.data_quality import score_data_quality
from domain.lap_detection import detect_laps
from domain.session_metrics import extract_session_metrics
from domain.timestamp_alignment import align_canonical_timestamps
from domain.track_layout import TrackLayout
from infra.db import make_engine, make_session_factory
from infra.orm import Lap as LapRow
from infra.orm import Session as SessionRow
from infra.orm import SessionRecord as SessionRecordRow
from infra.orm import TelemetryArtifact
from infra.storage import BlobStoreProtocol

DEFAULT_EXPECTED_CHANNELS = frozenset(
    {"speed_mps", "throttle_pct", "brake_pct", "lap_number", "lap_dist_m"}
)


async def run_session_pipeline(
    session_id: UUID,
    blob_store: BlobStoreProtocol,
) -> None:
    """Run the full processing pipeline for one session.

    Idempotent: deletes existing Lap rows and overwrites the
    SessionRecord payload, so a Celery retry that re-enters this
    function leaves the database in the same end state.
    """
    db_url = os.environ["DATABASE_URL"]
    engine = make_engine(db_url)
    try:
        factory = make_session_factory(engine)
        async with factory() as db:
            session_row = (
                await db.execute(
                    select(SessionRow).where(SessionRow.id == session_id)
                )
            ).scalar_one_or_none()
            if session_row is None:
                raise ValueError(f"session_not_found: {session_id}")

            artifacts = list(
                (
                    await db.execute(
                        select(TelemetryArtifact).where(
                            TelemetryArtifact.session_id == session_id
                        )
                    )
                ).scalars()
            )

            samples: list[dict[str, Any]] = []
            for artifact in artifacts:
                chunk = blob_store.get(artifact.s3_key)
                if chunk is None:
                    continue
                samples.extend(parse_bundle_v2(chunk))

            aligned = align_canonical_timestamps(samples)
            track_layout = TrackLayout(name="placeholder", length_m=0.0)
            laps = list(detect_laps(aligned, track_layout))
            metrics = extract_session_metrics(laps, aligned)
            quality = score_data_quality(
                aligned,
                expected_channels=set(DEFAULT_EXPECTED_CHANNELS),
            )

            existing_laps = list(
                (
                    await db.execute(
                        select(LapRow).where(LapRow.session_id == session_id)
                    )
                ).scalars()
            )
            for old in existing_laps:
                await db.delete(old)

            for lap in laps:
                db.add(
                    LapRow(
                        session_id=session_id,
                        lap_number=lap.lap_number,
                        time_ms=lap.time_ms,
                        sector_times=[],
                        valid=lap.valid,
                    )
                )

            payload: dict[str, Any] = {
                "metrics": {
                    "best_lap_ms": metrics.best_lap_ms,
                    "avg_lap_ms": metrics.avg_lap_ms,
                    "consistency_ms": metrics.consistency_ms,
                    "peak_speed_kph": metrics.peak_speed_kph,
                },
                "data_quality": {
                    "score": quality.score,
                    "channel_completeness": quality.channel_completeness,
                    "sample_gaps_count": quality.sample_gaps_count,
                    "sample_rate_stability": quality.sample_rate_stability,
                },
            }
            existing_record = (
                await db.execute(
                    select(SessionRecordRow).where(
                        SessionRecordRow.session_id == session_id
                    )
                )
            ).scalar_one_or_none()
            if existing_record is None:
                db.add(
                    SessionRecordRow(
                        session_id=session_id, payload=payload
                    )
                )
            else:
                existing_record.payload = payload

            session_row.status = "ready"
            await db.commit()
    finally:
        await engine.dispose()
