import json
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from lap_schemas.v1 import SessionIngestAccepted, SessionManifest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import (
    get_blob_store,
    get_current_user,
    get_db,
    get_idempotency_cache,
    get_task_dispatcher,
)
from domain.checksum_validator import (
    ChecksumMismatchError,
    verify_chunk_checksum,
)
from infra.idempotency_cache import IdempotencyCache
from infra.orm import Session as SessionRow
from infra.orm import TelemetryArtifact
from infra.orm import User as UserRow
from infra.storage import BlobStoreProtocol
from infra.task_dispatcher import TaskDispatcherProtocol

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


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
    _ = user
    if idempotency_key is None or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="idempotency_key_required",
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
            detail="manifest_invalid_json",
        ) from exc
    try:
        parsed_manifest = SessionManifest.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="manifest_invalid",
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
                detail="chunk_filename_invalid",
            )
        payload = await chunk.read()
        try:
            digest_hex = verify_chunk_checksum(
                filename, payload, expected_checksums
            )
        except ChecksumMismatchError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="chunk_checksum_mismatch",
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
