"""T005 (sprint2-backend) — Compare Case CRUD.

POST   /api/v1/compare_cases       create a saved comparison
GET    /api/v1/compare_cases/{id}  fetch one
DELETE /api/v1/compare_cases/{id}  remove one (204)

Cross-driver isolation: every load returns 404 for unowned cases
(not 403) so attackers can't enumerate IDs they don't own —
mirrors `prescriptions._load_owned_prescription` and
`sessions.get_session`.

T006 will add an auto-updating outcome block on GET when the
referenced sessions reprocess; that's deliberately separate so
T005 stays a clean CRUD shell.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from lap_schemas.v1 import (
    CompareCase,
    CompareCaseCreate,
    CompareCaseSessionSnapshot,
)
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from infra.orm import CompareCase as CompareCaseRow
from infra.orm import DriverProfile as DriverProfileRow
from infra.orm import SessionRecord as SessionRecordRow
from infra.orm import User as UserRow

router = APIRouter(prefix="/api/v1/compare_cases", tags=["compare-cases"])


async def _resolve_driver_id(
    db: AsyncSession, user_id: UUID
) -> UUID | None:
    return (
        await db.execute(
            select(DriverProfileRow.id).where(
                DriverProfileRow.user_id == user_id
            )
        )
    ).scalar_one_or_none()


def _serialize(
    row: CompareCaseRow,
    *,
    sessions: tuple[CompareCaseSessionSnapshot, ...] | None = None,
) -> CompareCase:
    return CompareCase(
        id=row.id,
        driver_id=row.driver_id,
        session_ids=tuple(UUID(s) for s in row.session_ids),
        rationale=row.rationale,
        created_at=row.created_at,
        sessions=sessions,
    )


async def _build_session_snapshot(
    db: AsyncSession, session_ids: list[str]
) -> tuple[CompareCaseSessionSnapshot, ...]:
    """T006 — for each referenced session, fetch the latest
    SessionRecord and synthesize a per-session snapshot. Sessions
    without a SessionRecord (still processing) get nulls.

    Recomputed on every GET — no cache to invalidate, so a
    SessionRecord update is reflected by the next request."""
    uuid_ids = [UUID(s) for s in session_ids]
    rows = (
        (
            await db.execute(
                select(SessionRecordRow).where(
                    SessionRecordRow.session_id.in_(uuid_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    by_session: dict[UUID, SessionRecordRow] = {
        row.session_id: row for row in rows
    }
    snapshots: list[CompareCaseSessionSnapshot] = []
    for sid in uuid_ids:
        record = by_session.get(sid)
        if record is None:
            snapshots.append(CompareCaseSessionSnapshot(session_id=sid))
            continue
        payload = record.payload or {}
        snapshots.append(
            CompareCaseSessionSnapshot(
                session_id=sid,
                processed_at=payload.get("processed_at"),
                best_lap_ms=payload.get("best_lap_ms"),
                total_laps=payload.get("total_laps"),
                valid_laps=payload.get("valid_laps"),
            )
        )
    return tuple(snapshots)


async def _load_owned(
    db: AsyncSession, *, case_id: UUID, driver_id: UUID
) -> CompareCaseRow:
    """Fetch a case scoped to driver_id; 404 if missing OR
    owned by someone else (no enumeration via existence leakage)."""
    row = (
        await db.execute(
            select(CompareCaseRow).where(CompareCaseRow.id == case_id)
        )
    ).scalar_one_or_none()
    if row is None or row.driver_id != driver_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="compare case not found",
        )
    return row


@router.post(
    "", status_code=status.HTTP_201_CREATED, response_model=CompareCase
)
async def create_compare_case(
    body: CompareCaseCreate,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CompareCase:
    driver_id = await _resolve_driver_id(db, user.id)
    if driver_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no driver profile",
        )
    row = CompareCaseRow(
        driver_id=driver_id,
        session_ids=[str(s) for s in body.session_ids],
        rationale=body.rationale,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _serialize(row)


@router.get("/{case_id}", response_model=CompareCase)
async def get_compare_case(
    case_id: UUID,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CompareCase:
    driver_id = await _resolve_driver_id(db, user.id)
    if driver_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="compare case not found",
        )
    row = await _load_owned(db, case_id=case_id, driver_id=driver_id)
    # T006 — auto-updating snapshot of referenced SessionRecords.
    snapshot = await _build_session_snapshot(db, row.session_ids)
    return _serialize(row, sessions=snapshot)


@router.delete("/{case_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_compare_case(
    case_id: UUID,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    driver_id = await _resolve_driver_id(db, user.id)
    if driver_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="compare case not found",
        )
    # _load_owned raises 404 if absent or not owned — same as GET.
    await _load_owned(db, case_id=case_id, driver_id=driver_id)
    await db.execute(
        delete(CompareCaseRow).where(CompareCaseRow.id == case_id)
    )
    await db.commit()
