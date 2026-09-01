"""Fail-closed authentication for requests signed by the Admin Web BFF."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import Depends, Request

from backend.core.config import Settings
from backend.core.errors import ApiError

AdminRole = Literal["viewer", "operator", "admin"]

ROLE_LEVEL: dict[AdminRole, int] = {
    "viewer": 1,
    "operator": 2,
    "admin": 3,
}


@dataclass(frozen=True, slots=True)
class AdminIdentity:
    actor_id: str
    role: AdminRole
    request_id: UUID


def canonical_admin_request(
    *,
    method: str,
    path_with_query: str,
    content_sha256: str,
    actor: str,
    role: str,
    timestamp: str,
    request_id: str,
) -> bytes:
    return "\n".join(
        (
            method.upper(),
            path_with_query,
            content_sha256,
            actor,
            role,
            timestamp,
            request_id,
        )
    ).encode("utf-8")


def _authentication_error() -> ApiError:
    return ApiError(
        status_code=401,
        reason="ADMIN_AUTHENTICATION_REQUIRED",
        detail="Admin authentication is missing, expired, or invalid.",
        retryable=False,
    )


def _normalized_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(key).lower(): str(value).strip() for key, value in headers.items()}


def verify_admin_request(
    *,
    settings: Settings,
    method: str,
    path_with_query: str,
    body: bytes,
    headers: Mapping[str, str],
    now: datetime | None = None,
) -> AdminIdentity:
    service_token = settings.snapshot_admin_service_token
    identity_secret = settings.snapshot_admin_identity_secret
    if service_token is None or identity_secret is None:
        raise ApiError(
            status_code=503,
            reason="ADMIN_AUTH_NOT_CONFIGURED",
            detail="The Admin authentication boundary is not configured.",
            retryable=False,
        )

    values = _normalized_headers(headers)
    authorization = values.get("authorization", "")
    scheme, separator, provided_token = authorization.partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not hmac.compare_digest(
            provided_token,
            service_token.get_secret_value(),
        )
    ):
        raise _authentication_error()

    actor = values.get("x-admin-actor", "")
    role = values.get("x-admin-role", "")
    timestamp = values.get("x-admin-timestamp", "")
    request_id_text = values.get("x-admin-request-id", "")
    content_sha256 = values.get("x-admin-content-sha256", "")
    provided_signature = values.get("x-admin-signature", "")

    if (
        not actor
        or len(actor) > 255
        or any(ord(character) < 32 or ord(character) == 127 for character in actor)
        or role not in ROLE_LEVEL
    ):
        raise _authentication_error()

    try:
        signed_at = int(timestamp)
        request_id = UUID(request_id_text)
    except (TypeError, ValueError) as exc:
        raise _authentication_error() from exc

    current_time = now or datetime.now(timezone.utc)
    signature_age = abs(int(current_time.timestamp()) - signed_at)
    if signature_age > settings.snapshot_admin_signature_max_age_seconds:
        raise _authentication_error()

    actual_content_sha256 = hashlib.sha256(body).hexdigest()
    if not hmac.compare_digest(content_sha256, actual_content_sha256):
        raise _authentication_error()

    canonical = canonical_admin_request(
        method=method,
        path_with_query=path_with_query,
        content_sha256=actual_content_sha256,
        actor=actor,
        role=role,
        timestamp=timestamp,
        request_id=request_id_text,
    )
    expected_signature = hmac.new(
        identity_secret.get_secret_value().encode("utf-8"),
        canonical,
        hashlib.sha256,
    ).hexdigest()
    if not provided_signature or not hmac.compare_digest(
        provided_signature,
        expected_signature,
    ):
        raise _authentication_error()

    return AdminIdentity(
        actor_id=actor,
        role=cast(AdminRole, role),
        request_id=request_id,
    )


async def get_admin_identity(request: Request) -> AdminIdentity:
    raw_query = request.scope.get("query_string", b"")
    path_with_query = request.url.path
    if raw_query:
        path_with_query = f"{path_with_query}?{raw_query.decode('latin-1')}"
    identity = verify_admin_request(
        settings=request.app.state.settings,
        method=request.method,
        path_with_query=path_with_query,
        body=await request.body(),
        headers=request.headers,
    )
    request.state.admin_identity = identity
    request.state.request_id = str(identity.request_id)
    return identity


def require_role(identity: AdminIdentity, required_role: str) -> AdminIdentity:
    if required_role not in ROLE_LEVEL or ROLE_LEVEL[identity.role] < ROLE_LEVEL[required_role]:
        raise ApiError(
            status_code=403,
            reason="ADMIN_PERMISSION_DENIED",
            detail="The authenticated Admin user does not have permission for this action.",
            retryable=False,
        )
    return identity


def require_admin_role(required_role: AdminRole):
    async def dependency(
        identity: Annotated[AdminIdentity, Depends(get_admin_identity)],
    ) -> AdminIdentity:
        return require_role(identity, required_role)

    return dependency
