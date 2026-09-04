"""Admin Audit Log routes."""

from __future__ import annotations

from fastapi import APIRouter, Query

from backend.features.admin.common import (
    Administrator,
    DbSession,
)
from backend.features.admin.pagination import decode_cursor, paginate
from backend.features.admin.schemas import (
    AuditLogListResponse,
    AuditLogResponse,
)
from backend.features.admin.store import AdminStore

router = APIRouter()


@router.get("/audit-logs", response_model=AuditLogListResponse)
async def list_audit_logs(
    session: DbSession,
    _identity: Administrator,
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = None,
) -> AuditLogListResponse:
    offset = decode_cursor(cursor)
    rows = await AdminStore(session).list_audit_logs(limit=limit + 1, offset=offset)
    entries, next_cursor = paginate(rows, limit=limit, offset=offset)
    return AuditLogListResponse(
        items=[AuditLogResponse.model_validate(item) for item in entries],
        next_cursor=next_cursor,
    )
