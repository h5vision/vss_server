"""Audit logging persistence helper for admin actions."""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from backend.infrastructure.database.models.audit import AuditLog


async def record_audit(
    session: AsyncSession,
    *,
    request_id: UUID | str,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    outcome: str = "succeeded",
    reason: str | None = None,
    detail: str | None = None,
    before_json: dict[str, Any] | None = None,
    after_json: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    req_uuid = UUID(str(request_id)) if isinstance(request_id, (str, UUID)) else uuid.uuid4()
    log_entry = AuditLog(
        audit_id=uuid.uuid4(),
        request_id=req_uuid,
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        outcome=outcome,
        reason=reason,
        detail=detail,
        before_json=before_json,
        after_json=after_json,
        details=details,
    )
    session.add(log_entry)
    await session.flush()
    return log_entry
