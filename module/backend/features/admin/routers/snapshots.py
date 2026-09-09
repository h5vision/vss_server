"""Admin Snapshot routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from backend.core.errors import ApiError
from backend.features.admin.audit import record_audit
from backend.features.admin.common import (
    DbSession,
    Operator,
    Viewer,
    _snapshot_detail,
    _snapshot_summary,
)
from backend.features.admin.pagination import decode_cursor, paginate
from backend.features.admin.store import AdminStore
from backend.features.snapshots.schemas import (
    SnapshotDetailResponse,
    SnapshotIndexResponse,
    SnapshotListResponse,
    SnapshotRetryResponse,
    SnapshotState,
)

router = APIRouter()


@router.get("/snapshots", response_model=SnapshotListResponse)
async def list_snapshots(
    session: DbSession,
    _identity: Viewer,
    repository_id: UUID | None = None,
    tracked_branch_id: UUID | None = None,
    state: SnapshotState | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = None,
) -> SnapshotListResponse:
    offset = decode_cursor(cursor)
    rows = await AdminStore(session).list_snapshots(
        repository_id=repository_id,
        tracked_branch_id=tracked_branch_id,
        state=state,
        limit=limit + 1,
        offset=offset,
    )
    snapshots, next_cursor = paginate(rows, limit=limit, offset=offset)
    return SnapshotListResponse(
        items=[_snapshot_summary(item) for item in snapshots],
        next_cursor=next_cursor,
    )


@router.get("/snapshots/{snapshot_id}", response_model=SnapshotDetailResponse)
async def get_snapshot(snapshot_id: UUID, session: DbSession, _identity: Viewer):
    snapshot = await AdminStore(session).get_snapshot_detail(snapshot_id)
    if snapshot is None:
        raise ApiError(
            status_code=404,
            reason="SNAPSHOT_NOT_FOUND",
            detail="The requested Snapshot was not found.",
            retryable=False,
        )
    return _snapshot_detail(snapshot)


@router.post("/snapshots/{snapshot_id}/index", response_model=SnapshotIndexResponse)
async def index_snapshot(
    snapshot_id: UUID,
    request: Request,
    response: Response,
    session: DbSession,
    identity: Operator,
) -> SnapshotIndexResponse:
    service = getattr(request.app.state, "snapshot_index_service", None)
    if service is None:
        raise ApiError(
            status_code=503,
            reason="ADMIN_DATABASE_UNAVAILABLE",
            detail="Snapshot Index is unavailable because the database is not configured.",
            retryable=True,
        )
    outcome = await service.index(snapshot_id, request_id=identity.request_id)
    response.status_code = outcome.status_code
    await record_audit(
        session,
        request_id=identity.request_id,
        actor=identity.actor_id,
        action="index_snapshot",
        target_type="snapshot",
        target_id=str(snapshot_id),
        after_json=outcome.body.model_dump(mode="json"),
    )
    return outcome.body


@router.post("/snapshots/{snapshot_id}/retry", response_model=SnapshotRetryResponse)
async def retry_snapshot(
    snapshot_id: UUID,
    request: Request,
    response: Response,
    session: DbSession,
    identity: Operator,
) -> SnapshotRetryResponse:
    service = getattr(request.app.state, "snapshot_retry_service", None)
    if service is None:
        raise ApiError(
            status_code=503,
            reason="ADMIN_DATABASE_UNAVAILABLE",
            detail="Snapshot retry is unavailable because the database is not configured.",
            retryable=True,
        )
    outcome = await service.retry(snapshot_id, request_id=identity.request_id)
    response.status_code = outcome.status_code
    await record_audit(
        session,
        request_id=identity.request_id,
        actor=identity.actor_id,
        action="retry_snapshot",
        target_type="snapshot",
        target_id=str(snapshot_id),
        after_json=outcome.body.model_dump(mode="json"),
    )
    return outcome.body
