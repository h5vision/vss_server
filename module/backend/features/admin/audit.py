"""Persistence helpers for security-relevant Admin mutations."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from backend.infrastructure.database.models import AuditLog


async def record_audit(
    session: AsyncSession,
    *,
    request_id: UUID,
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
    entry = AuditLog(
        request_id=request_id,
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
    session.add(entry)
    await session.flush()
    return entry
