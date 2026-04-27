import json
import time
from dataclasses import dataclass
from typing import Annotated, cast
from uuid import UUID

import uuid6
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from lap_schemas.v1 import (
    NextActionsResponse,
    SessionCoachingView,
    SessionDetail,
    SessionIngestAccepted,
    SessionManifest,
    StrategyEvent,
    StrategyEventCreate,
    StrategyHistoryEntry,
    StrategyHistoryList,
    StrategyPlan,
    StrategyPlanCreate,
    StrategySimulateRequest,
    StrategySimulateResponse,
    StrategyTemplate,
    StrategyTemplateCreate,
    StrategyTemplateList,
    StrategyTimelineResponse,
    StrategyVisualization,
)
from lap_schemas.v1.action_prescription import ActionPrescriptionListItem
from lap_schemas.v1.coaching_insight import CoachingInsight
from lap_schemas.v1.session import SessionStatus
from lap_schemas.v1.weakness_episode import WeaknessEpisode as WeaknessEpisodeSchema
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import (
    get_blob_store,
    get_current_user,
    get_db,
    get_idempotency_cache,
    get_posthog_client,
    get_task_dispatcher,
)
from app.error_codes import ErrorCode
from domain.checksum_validator import (
    ChecksumMismatchError,
    verify_chunk_checksum,
)
from domain.strategy_caution_recalc import (
    compute_strategy_divergence_with_cautions,
)
from domain.strategy_execution_score import (
    compute_strategy_execution_score,
)
from infra.idempotency_cache import IdempotencyCache
from infra.metrics import ingest_duration_seconds, sessions_ingested_total
from infra.orm import ActionPrescription as ActionPrescriptionRow
from infra.orm import CoachingInsight as CoachingInsightRow
from infra.orm import DriverProfile as DriverProfileRow
from infra.orm import Session as SessionRow
from infra.orm import StrategyEvent as StrategyEventRow
from infra.orm import StrategyPlan as StrategyPlanRow
from infra.orm import StrategyTemplate as StrategyTemplateRow
from infra.orm import TelemetryArtifact
from infra.orm import User as UserRow
from infra.orm import WeaknessEpisode as WeaknessEpisodeRow
from infra.posthog import PosthogClientProtocol
from infra.storage import BlobStoreProtocol
from infra.task_dispatcher import TaskDispatcherProtocol

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])
driver_router = APIRouter(prefix="/api/v1/driver", tags=["sessions"])


@router.post(
    "/ingest",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SessionIngestAccepted,
)
async def ingest_session(
    manifest: Annotated[str, Form()],
    user: Annotated[UserRow, Depends(get_current_user)],
    cache: Annotated[IdempotencyCache, Depends(get_idempotency_cache)],
    blob_store: Annotated[BlobStoreProtocol, Depends(get_blob_store)],
    db: Annotated[AsyncSession, Depends(get_db)],
    task_dispatcher: Annotated[
        TaskDispatcherProtocol, Depends(get_task_dispatcher)
    ],
    posthog_client: Annotated[
        PosthogClientProtocol, Depends(get_posthog_client)
    ],
    chunks: Annotated[list[UploadFile] | None, File()] = None,
    idempotency_key: Annotated[
        str | None, Header(alias="idempotency-key")
    ] = None,
) -> Response | SessionIngestAccepted:
    # T097: validate manifest against lap_schemas.v1.SessionManifest.
    # T098: require Idempotency-Key header.
    # T099: replay cached response on key hit (24h TTL); cache write
    # happens only on success so a malformed body can't poison the slot.
    # T100: persist each chunk to the configured blob store under
    # `{session_id}/{filename}`. Path traversal in chunk filenames is
    # rejected with 400 at the boundary.
    started_at = time.monotonic()
    if idempotency_key is None or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
        )
    cached = cache.get(idempotency_key)
    if cached is not None:
        return Response(
            content=cached,
            status_code=status.HTTP_202_ACCEPTED,
            media_type="application/json",
        )
    try:
        data = json.loads(manifest)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=ErrorCode.MANIFEST_INVALID_JSON,
        ) from exc
    try:
        parsed_manifest = SessionManifest.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=ErrorCode.MANIFEST_INVALID,
        ) from exc

    session_id = parsed_manifest.session_id
    expected_checksums = parsed_manifest.telemetry_checksums
    # Two-pass: read + verify ALL chunks before persisting any. A
    # mid-loop checksum failure must not leave half the upload on disk
    # nor any partial DB rows.
    verified: list[tuple[str, bytes, str]] = []
    for chunk in chunks or []:
        filename = chunk.filename or ""
        if (
            not filename
            or "/" in filename
            or "\\" in filename
            or filename in {".", ".."}
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ErrorCode.CHUNK_FILENAME_INVALID,
            )
        payload = await chunk.read()
        try:
            digest_hex = verify_chunk_checksum(
                filename, payload, expected_checksums
            )
        except ChecksumMismatchError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=ErrorCode.CHUNK_CHECKSUM_MISMATCH,
            ) from exc
        verified.append((filename, payload, digest_hex))

    # T102: persist a Session row (status="uploaded") and one
    # TelemetryArtifact row per chunk. Manifest is stored as JSON so
    # downstream Celery (T103) can read it without re-parsing the
    # multipart body.
    session_row = SessionRow(
        id=session_id,
        driver_id=parsed_manifest.driver_id,
        manifest=parsed_manifest.model_dump(mode="json"),
        status="uploaded",
    )
    db.add(session_row)
    session_id_str = str(session_id)
    for filename, payload, digest_hex in verified:
        db.add(
            TelemetryArtifact(
                session_id=session_id,
                s3_key=f"{session_id_str}/{filename}",
                sha256=digest_hex,
                size_bytes=len(payload),
            )
        )
    await db.commit()

    for filename, payload, _ in verified:
        blob_store.put(f"{session_id_str}/{filename}", payload)

    # T103: queue downstream processing. Dispatcher is abstracted so
    # T107 can swap to a real Celery backend without touching callers.
    job_id = task_dispatcher.dispatch_session_ingest(session_id)

    # T176: Prometheus counter + duration histogram. Increment after
    # dispatch so we only count sessions that made it to the queue.
    sessions_ingested_total.inc()
    ingest_duration_seconds.observe(time.monotonic() - started_at)

    # T170: emit `session_ingested` to PostHog after dispatch — fire
    # and forget; analytics never blocks ingestion. The client is a
    # no-op stand-in in dev/CI (no network, no events stored).
    posthog_client.capture(
        event="session_ingested",
        distinct_id=str(user.id),
        properties={
            "session_id": str(session_id),
            "source": parsed_manifest.source,
            "environment_tag": parsed_manifest.environment_tag,
        },
    )

    # T104: response carries the session id, the dispatcher's job id,
    # and the SSE stream URL the client subscribes to for progress.
    response = SessionIngestAccepted(
        status="accepted",
        session_id=session_id,
        ingest_job_id=job_id,
        status_stream_url=f"/api/v1/jobs/{job_id}/events",
    )
    cache.put(idempotency_key, response.model_dump_json().encode())
    return response


@router.get("/{session_id}", response_model=SessionDetail)
async def get_session(
    session_id: UUID,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionDetail:
    # T123: expose Session.status (and failure_reason) so clients
    # polling after a Celery job sees the transition to "ready" or
    # "failed". Authorization: only the owning driver can read.
    # Both unknown and non-owned sessions return 404 (don't leak
    # existence to non-owners).
    profile = (
        await db.execute(
            select(DriverProfileRow).where(
                DriverProfileRow.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    row = (
        await db.execute(
            select(SessionRow).where(SessionRow.id == session_id)
        )
    ).scalar_one_or_none()
    if row is None or profile is None or row.driver_id != profile.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorCode.SESSION_NOT_FOUND,
        )
    return SessionDetail(
        id=row.id,
        driver_id=row.driver_id,
        status=cast(SessionStatus, row.status),
        created_at=row.created_at,
        failure_reason=row.failure_reason,
    )


# T224 / WA-009 — post-session debrief bundle. Returns
# WeaknessEpisodes touched by this session + open prescriptions
# linked to them. Replaces the webapp's old SessionDebrief
# client-side heuristic.
_OPEN_PRESCRIPTION_STATUSES = ("proposed", "accepted", "in_progress")


@router.get(
    "/{session_id}/next_actions",
    response_model=NextActionsResponse,
)
async def get_session_next_actions(
    session_id: UUID,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> NextActionsResponse:
    profile = (
        await db.execute(
            select(DriverProfileRow).where(
                DriverProfileRow.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    session_row = (
        await db.execute(
            select(SessionRow).where(SessionRow.id == session_id)
        )
    ).scalar_one_or_none()
    if (
        session_row is None
        or profile is None
        or session_row.driver_id != profile.id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorCode.SESSION_NOT_FOUND,
        )
    # JSONB containment isn't standard SQL so filter sessions_affected
    # in Python — episode counts per driver are bounded.
    session_str = str(session_id)
    candidate_episodes = (
        await db.execute(
            select(WeaknessEpisodeRow).where(
                WeaknessEpisodeRow.driver_id == profile.id
            )
        )
    ).scalars().all()
    matching_episodes = [
        ep for ep in candidate_episodes
        if session_str in ep.sessions_affected
    ]
    episode_ids = [ep.id for ep in matching_episodes]
    open_prescriptions = (
        (
            await db.execute(
                select(ActionPrescriptionRow)
                .where(
                    ActionPrescriptionRow.weakness_episode_id.in_(
                        episode_ids
                    )
                )
                .where(
                    ActionPrescriptionRow.status.in_(
                        _OPEN_PRESCRIPTION_STATUSES
                    )
                )
            )
        ).scalars().all()
        if episode_ids
        else []
    )
    return NextActionsResponse(
        session_id=session_id,
        weakness_episodes=tuple(
            WeaknessEpisodeSchema.model_validate(
                ep, from_attributes=True
            )
            for ep in matching_episodes
        ),
        action_prescriptions=tuple(
            ActionPrescriptionListItem(
                id=p.id,
                driver_id=p.driver_id,
                weakness_episode_id=p.weakness_episode_id,
                created_at=p.created_at,
                priority=p.priority,
                title=p.title,
                rationale=p.rationale,
                drill_template_id=p.drill_template_id,
                success_condition=p.success_condition,
                status=p.status,
                accepted_at=p.accepted_at,
            )
            for p in open_prescriptions
        ),
    )


# T010 (sprint2-backend) — retrospective coaching view for ONE
# session. Distinct from /next_actions (which is forward-looking
# debrief): this returns what the rules engine actually said about
# this session, including resolved/skipped artifacts.
#
# Weakness linkage uses WeaknessEpisode.linked_insights (insight ids
# the episode incorporated as evidence) — filtered in Python because
# JSONB array containment isn't standard SQL, and the cardinality
# (episodes per driver) is bounded.
@router.get(
    "/{session_id}/coaching",
    response_model=SessionCoachingView,
)
async def get_session_coaching(
    session_id: UUID,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionCoachingView:
    profile = (
        await db.execute(
            select(DriverProfileRow).where(
                DriverProfileRow.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    session_row = (
        await db.execute(
            select(SessionRow).where(SessionRow.id == session_id)
        )
    ).scalar_one_or_none()
    if (
        session_row is None
        or profile is None
        or session_row.driver_id != profile.id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorCode.SESSION_NOT_FOUND,
        )

    insight_rows = list(
        (
            await db.execute(
                select(CoachingInsightRow).where(
                    CoachingInsightRow.session_id == session_id
                )
            )
        )
        .scalars()
        .all()
    )
    insight_ids = {str(r.id) for r in insight_rows}
    rx_ids = [
        r.suggested_prescription_id
        for r in insight_rows
        if r.suggested_prescription_id is not None
    ]

    rx_rows = (
        list(
            (
                await db.execute(
                    select(ActionPrescriptionRow).where(
                        ActionPrescriptionRow.id.in_(rx_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        if rx_ids
        else []
    )

    weakness_candidates = list(
        (
            await db.execute(
                select(WeaknessEpisodeRow).where(
                    WeaknessEpisodeRow.driver_id == profile.id
                )
            )
        )
        .scalars()
        .all()
    )
    matching_weaknesses = [
        ep
        for ep in weakness_candidates
        if any(linked in insight_ids for linked in (ep.linked_insights or []))
    ]

    return SessionCoachingView(
        session_id=session_id,
        insights=tuple(_insight_schema(r) for r in insight_rows),
        weaknesses_touched=tuple(
            WeaknessEpisodeSchema.model_validate(
                ep, from_attributes=True
            )
            for ep in matching_weaknesses
        ),
        prescriptions_generated=tuple(
            ActionPrescriptionListItem(
                id=p.id,
                driver_id=p.driver_id,
                weakness_episode_id=p.weakness_episode_id,
                created_at=p.created_at,
                priority=p.priority,
                title=p.title,
                rationale=p.rationale,
                drill_template_id=p.drill_template_id,
                success_condition=p.success_condition,
                status=p.status,
                accepted_at=p.accepted_at,
            )
            for p in rx_rows
        ),
    )


def _insight_schema(row: CoachingInsightRow) -> CoachingInsight:
    return CoachingInsight(
        id=row.id,
        session_id=row.session_id,
        driver_id=row.driver_id,
        created_at=row.created_at,
        rule_id=row.rule_id,
        rule_version=row.rule_version,
        observation=row.observation,
        evidence=tuple(row.evidence or []),
        likely_cause=row.likely_cause,
        severity=row.severity,
        confidence=row.confidence,
        corner_tag=row.corner_tag,
        suggested_prescription_id=row.suggested_prescription_id,
        flagged_false_positive=row.flagged_false_positive,
    )


@router.post(
    "/{session_id}/strategy/plan",
    response_model=StrategyPlan,
    status_code=status.HTTP_201_CREATED,
    responses={
        404: {"description": "session_not_found (or not owned)"},
        409: {"description": "strategy_plan_already_exists"},
    },
)
async def create_strategy_plan(
    session_id: UUID,
    payload: StrategyPlanCreate,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StrategyPlan:
    """T098 — set the pre-race strategy plan for a session.

    Authz: only the owning driver can write; same 404 for
    "session doesn't exist" vs "exists but not yours" so the
    UUID space isn't an enumeration oracle.

    Idempotency-by-conflict: T096's UNIQUE(session_id) enforces
    one canonical plan per session. A second POST returns 409
    rather than silently overwriting — re-plans during the race
    go through StrategyEvent (T097, kind='plan'), preserving
    the audit trail.
    """
    profile = (
        await db.execute(
            select(DriverProfileRow).where(
                DriverProfileRow.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    session_row = (
        await db.execute(
            select(SessionRow).where(SessionRow.id == session_id)
        )
    ).scalar_one_or_none()
    if (
        session_row is None
        or profile is None
        or session_row.driver_id != profile.id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorCode.SESSION_NOT_FOUND,
        )
    existing = (
        await db.execute(
            select(StrategyPlanRow).where(
                StrategyPlanRow.session_id == session_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ErrorCode.STRATEGY_PLAN_ALREADY_EXISTS,
        )
    row = StrategyPlanRow(
        id=uuid6.uuid7(),
        session_id=session_id,
        planned_stints=payload.planned_stints,
        planned_fuel_liters=payload.planned_fuel_liters,
        planned_pit_laps=list(payload.planned_pit_laps),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return StrategyPlan(
        id=row.id,
        session_id=row.session_id,
        created_at=row.created_at,
        planned_stints=row.planned_stints,
        planned_fuel_liters=row.planned_fuel_liters,
        planned_pit_laps=tuple(row.planned_pit_laps),
    )


@router.post(
    "/{session_id}/strategy/events",
    response_model=StrategyEvent,
    status_code=status.HTTP_201_CREATED,
    responses={404: {"description": "session_not_found (or not owned)"}},
)
async def create_strategy_event(
    session_id: UUID,
    payload: StrategyEventCreate,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StrategyEvent:
    """T099 — append a runtime strategy event to a session's
    timeline (T097 ORM).

    Authz: same pattern as the plan endpoint — only the owning
    driver can write; same 404 for "session doesn't exist" vs
    "exists but not yours" so the UUID space isn't an
    enumeration oracle.

    Append-only: no uniqueness check; two same-kind events on
    the same lap are valid (chaotic-race scenarios per T097
    design — formation restarts, double FCYs).
    """
    profile = (
        await db.execute(
            select(DriverProfileRow).where(
                DriverProfileRow.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    session_row = (
        await db.execute(
            select(SessionRow).where(SessionRow.id == session_id)
        )
    ).scalar_one_or_none()
    if (
        session_row is None
        or profile is None
        or session_row.driver_id != profile.id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorCode.SESSION_NOT_FOUND,
        )
    row = StrategyEventRow(
        id=uuid6.uuid7(),
        session_id=session_id,
        kind=payload.kind,
        at_lap=payload.at_lap,
        details=dict(payload.details),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return StrategyEvent(
        id=row.id,
        session_id=row.session_id,
        kind=row.kind,  # type: ignore[arg-type]
        at_lap=row.at_lap,
        details=row.details,
        created_at=row.created_at,
    )


@router.get(
    "/{session_id}/strategy/timeline",
    response_model=StrategyTimelineResponse,
    responses={404: {"description": "session_not_found (or not owned)"}},
)
async def get_strategy_timeline(
    session_id: UUID,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StrategyTimelineResponse:
    """T100 — combined plan + actual timeline. Returns the
    canonical pre-race plan (T096) plus the runtime event log
    (T097) ordered by lap. UI renders them side-by-side; T101+
    plan-vs-actual divergence scoring consumes the same shape.

    Authz: same pattern as POST endpoints — only the owning
    driver reads. Empty shape (plan=null, events=[]) for a
    session with no strategy data; distinct from 404 so the
    UI can show "no strategy recorded" vs "session not found".
    """
    profile = (
        await db.execute(
            select(DriverProfileRow).where(
                DriverProfileRow.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    session_row = (
        await db.execute(
            select(SessionRow).where(SessionRow.id == session_id)
        )
    ).scalar_one_or_none()
    if (
        session_row is None
        or profile is None
        or session_row.driver_id != profile.id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorCode.SESSION_NOT_FOUND,
        )
    plan_row = (
        await db.execute(
            select(StrategyPlanRow).where(
                StrategyPlanRow.session_id == session_id
            )
        )
    ).scalar_one_or_none()
    event_rows = (
        await db.execute(
            select(StrategyEventRow)
            .where(StrategyEventRow.session_id == session_id)
            .order_by(
                StrategyEventRow.at_lap.asc(),
                StrategyEventRow.created_at.asc(),
            )
        )
    ).scalars().all()
    return StrategyTimelineResponse(
        plan=(
            StrategyPlan(
                id=plan_row.id,
                session_id=plan_row.session_id,
                created_at=plan_row.created_at,
                planned_stints=plan_row.planned_stints,
                planned_fuel_liters=plan_row.planned_fuel_liters,
                planned_pit_laps=tuple(plan_row.planned_pit_laps),
            )
            if plan_row is not None
            else None
        ),
        events=tuple(
            StrategyEvent(
                id=ev.id,
                session_id=ev.session_id,
                kind=ev.kind,  # type: ignore[arg-type]
                at_lap=ev.at_lap,
                details=ev.details,
                created_at=ev.created_at,
            )
            for ev in event_rows
        ),
    )


@router.get(
    "/{session_id}/strategy/visualization",
    response_model=StrategyVisualization,
    responses={404: {"description": "session_not_found (or not owned)"}},
)
async def get_strategy_visualization(
    session_id: UUID,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StrategyVisualization:
    """T105 — webapp-facing aggregated read for the plan-vs-actual
    chart. Bundles T096 plan + actuals derived from T097 events +
    T102 execution_score. Returns the visualization-shaped tuples
    so the webapp doesn't reimplement the fuel-summing or
    stint-length-derivation math (which must stay aligned with
    T101's divergence math).

    Authz: same pattern as T100 — only the owning driver reads.
    Empty shape (plan=null, all actuals empty/0, score=null) for
    a session with no strategy data; distinct from 404 so the UI
    distinguishes "no strategy recorded" from "session not found".
    """
    profile = (
        await db.execute(
            select(DriverProfileRow).where(
                DriverProfileRow.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    session_row = (
        await db.execute(
            select(SessionRow).where(SessionRow.id == session_id)
        )
    ).scalar_one_or_none()
    if (
        session_row is None
        or profile is None
        or session_row.driver_id != profile.id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorCode.SESSION_NOT_FOUND,
        )

    plan_row = (
        await db.execute(
            select(StrategyPlanRow).where(
                StrategyPlanRow.session_id == session_id
            )
        )
    ).scalar_one_or_none()

    pit_rows = (
        await db.execute(
            select(StrategyEventRow)
            .where(
                StrategyEventRow.session_id == session_id,
                StrategyEventRow.kind == "pit",
            )
            .order_by(StrategyEventRow.at_lap.asc())
        )
    ).scalars().all()
    stint_end_rows = (
        await db.execute(
            select(StrategyEventRow)
            .where(
                StrategyEventRow.session_id == session_id,
                StrategyEventRow.kind == "stint_end",
            )
            .order_by(StrategyEventRow.at_lap.asc())
        )
    ).scalars().all()

    actual_pit_laps = tuple(p.at_lap for p in pit_rows)
    # Mirrors T101's defensive `details.get("fuel_added_liters", 0)` —
    # tire-only stops legitimately omit the key and contribute 0.
    actual_fuel_total = sum(
        float(p.details.get("fuel_added_liters", 0) or 0)
        for p in pit_rows
    )
    # stint 1 = first end_lap; stint N = end_lap[N] - end_lap[N-1]
    stint_lengths: list[int] = []
    prev = 0
    for ev in stint_end_rows:
        stint_lengths.append(ev.at_lap - prev)
        prev = ev.at_lap

    return StrategyVisualization(
        plan=(
            StrategyPlan(
                id=plan_row.id,
                session_id=plan_row.session_id,
                created_at=plan_row.created_at,
                planned_stints=plan_row.planned_stints,
                planned_fuel_liters=plan_row.planned_fuel_liters,
                planned_pit_laps=tuple(plan_row.planned_pit_laps),
            )
            if plan_row is not None
            else None
        ),
        actual_pit_laps=actual_pit_laps,
        actual_fuel_liters_total=actual_fuel_total,
        actual_stint_lengths=tuple(stint_lengths),
        execution_score=(
            plan_row.execution_score if plan_row is not None else None
        ),
    )


@driver_router.get(
    "/strategy/history",
    response_model=StrategyHistoryList,
)
async def get_driver_strategy_history(
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> StrategyHistoryList:
    """T106 — driver-scoped list of recent sessions with a
    StrategyPlan (T096) attached, each entry carrying the plan
    summary and T102's persisted execution_score.

    Authz: only the caller's own sessions appear (driver_id
    filter on the join). Owned-but-no-plans returns 200 with
    an empty list — distinct from "driver not found".

    Ordering: session.created_at DESC (most recent first — the
    canonical "recent" interpretation; webapp scrolls down for
    older). Plans without an execution_score yet are still
    listed (None vs 0 distinction preserved).
    """
    profile = (
        await db.execute(
            select(DriverProfileRow).where(
                DriverProfileRow.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if profile is None:
        return StrategyHistoryList(entries=())

    rows = (
        await db.execute(
            select(StrategyPlanRow, SessionRow)
            .join(
                SessionRow,
                SessionRow.id == StrategyPlanRow.session_id,
            )
            .where(SessionRow.driver_id == profile.id)
            .order_by(SessionRow.created_at.desc())
            .limit(limit)
        )
    ).all()

    entries = tuple(
        StrategyHistoryEntry(
            session_id=plan.session_id,
            session_created_at=session.created_at,
            planned_stints=plan.planned_stints,
            planned_fuel_liters=plan.planned_fuel_liters,
            planned_pit_laps=tuple(plan.planned_pit_laps),
            execution_score=plan.execution_score,
        )
        for plan, session in rows
    )
    return StrategyHistoryList(entries=entries)


def _template_to_schema(row: StrategyTemplateRow) -> StrategyTemplate:
    return StrategyTemplate(
        id=row.id,
        created_at=row.created_at,
        name=row.name,
        planned_stints=row.planned_stints,
        planned_fuel_liters=row.planned_fuel_liters,
        planned_pit_laps=tuple(row.planned_pit_laps),
    )


@driver_router.post(
    "/strategy/templates",
    status_code=status.HTTP_201_CREATED,
    response_model=StrategyTemplate,
    responses={409: {"description": "duplicate_template_name"}},
)
async def create_strategy_template(
    body: StrategyTemplateCreate,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StrategyTemplate:
    """T107 — save a named StrategyPlan-shaped template for the
    caller. UNIQUE(driver_id, name) → 409 on duplicate name within
    the same driver. Per-driver, not global, so two users can each
    have a "Spa 24h" template."""
    profile = (
        await db.execute(
            select(DriverProfileRow).where(
                DriverProfileRow.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorCode.DRIVER_NOT_FOUND,
        )
    row = StrategyTemplateRow(
        driver_id=profile.id,
        name=body.name,
        planned_stints=body.planned_stints,
        planned_fuel_liters=body.planned_fuel_liters,
        planned_pit_laps=list(body.planned_pit_laps),
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ErrorCode.DUPLICATE_TEMPLATE_NAME,
        ) from exc
    await db.refresh(row)
    return _template_to_schema(row)


@driver_router.get(
    "/strategy/templates",
    response_model=StrategyTemplateList,
)
async def list_strategy_templates(
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StrategyTemplateList:
    """T107 — list the caller's saved templates ordered by
    created_at DESC. Driver-scoped: only the caller's templates
    appear (RLS via the driver_id WHERE clause)."""
    profile = (
        await db.execute(
            select(DriverProfileRow).where(
                DriverProfileRow.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if profile is None:
        return StrategyTemplateList(entries=())

    rows = (
        await db.execute(
            select(StrategyTemplateRow)
            .where(StrategyTemplateRow.driver_id == profile.id)
            .order_by(StrategyTemplateRow.created_at.desc())
        )
    ).scalars().all()

    return StrategyTemplateList(
        entries=tuple(_template_to_schema(r) for r in rows),
    )


@driver_router.delete(
    "/strategy/templates/{template_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"description": "template_not_found"}},
)
async def delete_strategy_template(
    template_id: UUID,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """T107 — delete the caller's named template. 404 status
    parity for unknown vs not-owned: prevents enumeration of
    other drivers' template ids via timing or status."""
    profile = (
        await db.execute(
            select(DriverProfileRow).where(
                DriverProfileRow.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorCode.TEMPLATE_NOT_FOUND,
        )
    row = (
        await db.execute(
            select(StrategyTemplateRow).where(
                StrategyTemplateRow.id == template_id,
                StrategyTemplateRow.driver_id == profile.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorCode.TEMPLATE_NOT_FOUND,
        )
    await db.execute(
        delete(StrategyTemplateRow).where(
            StrategyTemplateRow.id == template_id
        )
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@dataclass(frozen=True)
class _HypotheticalPlan:
    """Adapter exposing the StrategyPlanCreate-shaped fields the
    domain divergence functions read via Protocol. Keeps T101's
    primitive ignorant of Pydantic / ORM specifics."""
    planned_stints: int
    planned_fuel_liters: float
    planned_pit_laps: list[int]


@router.post(
    "/{session_id}/strategy/simulate",
    response_model=StrategySimulateResponse,
    responses={404: {"description": "session_not_found (or not owned)"}},
)
async def simulate_strategy(
    session_id: UUID,
    body: StrategySimulateRequest,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StrategySimulateResponse:
    """T109 — "what if I pitted 2 laps later?". Scores a
    hypothetical plan against the session's actual events.
    Read-only: no StrategyPlan is created (the canonical plan
    lives behind T098 POST and would 409 the simulator).

    Uses T108 caution-aware divergence so the simulated score
    reflects the cautions that actually occurred — matches the
    real-world counterfactual ("if I had pitted differently with
    the SAME race conditions").

    Authz: same 404 status parity as the read endpoints
    (other-driver session = "not found" prevents enumeration).
    """
    profile = (
        await db.execute(
            select(DriverProfileRow).where(
                DriverProfileRow.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    session_row = (
        await db.execute(
            select(SessionRow).where(SessionRow.id == session_id)
        )
    ).scalar_one_or_none()
    if (
        session_row is None
        or profile is None
        or session_row.driver_id != profile.id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorCode.SESSION_NOT_FOUND,
        )
    event_rows = (
        await db.execute(
            select(StrategyEventRow)
            .where(StrategyEventRow.session_id == session_id)
            .order_by(
                StrategyEventRow.at_lap.asc(),
                StrategyEventRow.created_at.asc(),
            )
        )
    ).scalars().all()
    plan = _HypotheticalPlan(
        planned_stints=body.planned_stints,
        planned_fuel_liters=body.planned_fuel_liters,
        planned_pit_laps=list(body.planned_pit_laps),
    )
    divergence = compute_strategy_divergence_with_cautions(
        plan=plan,
        events=event_rows,
        total_race_laps=body.total_race_laps,
    )
    score = compute_strategy_execution_score(
        divergence=divergence,
        planned_fuel_liters=body.planned_fuel_liters,
        total_race_laps=body.total_race_laps,
    )
    return StrategySimulateResponse(
        pit_lap_delta_total=divergence.pit_lap_delta_total,
        fuel_delta_liters=divergence.fuel_delta_liters,
        stint_duration_delta_laps=divergence.stint_duration_delta_laps,
        execution_score=score,
    )



