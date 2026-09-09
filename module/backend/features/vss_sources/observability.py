"""Operational visibility for inbound VSS-to-Module requests."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from backend.features.admin.audit import record_audit

_REDACTED = "<redacted>"
_SENSITIVE_QUERY_MARKERS = (
    "token",
    "secret",
    "password",
    "authorization",
    "credential",
    "api_key",
    "apikey",
)


def sanitize_query_params(items: Iterable[tuple[str, str]]) -> dict[str, Any]:
    """Preserve diagnostic query context without ever persisting credentials."""

    result: dict[str, Any] = {}
    for key, raw_value in items:
        normalized_key = key.strip()
        lowered = normalized_key.lower()
        value = (
            _REDACTED
            if any(marker in lowered for marker in _SENSITIVE_QUERY_MARKERS)
            else raw_value[:512]
        )
        existing = result.get(normalized_key)
        if existing is None:
            result[normalized_key] = value
        elif isinstance(existing, list):
            existing.append(value)
        else:
            result[normalized_key] = [existing, value]
    return result


async def record_vss_inbound_non_success(
    session: AsyncSession,
    *,
    request_id: UUID,
    method: str,
    path: str,
    status_code: int,
    query: dict[str, Any],
    elapsed_ms: float,
    client_host: str | None,
) -> None:
    """Persist one non-200/202 VSS inbound result in the existing audit store."""

    await record_audit(
        session,
        request_id=request_id,
        actor="vss-inbound",
        action="vss_inbound_request",
        target_type="vss_internal_route",
        target_id=path,
        outcome="denied" if status_code in {401, 403} else "failed",
        reason=f"HTTP_{status_code}",
        detail="VSS inbound request returned a status other than 200 or 202.",
        details={
            "method": method,
            "status_code": status_code,
            "query": query,
            "project_id": query.get("project_id"),
            "elapsed_ms": round(elapsed_ms, 1),
            "client_host": client_host,
        },
    )
