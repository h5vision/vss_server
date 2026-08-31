"""Admin authentication and RBAC dependency."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, Header, Request

from backend.core.config import Settings
from backend.core.errors import ApiError

AdminRole = Literal["viewer", "operator", "admin"]

ROLE_HIERARCHY: dict[AdminRole, int] = {
    "viewer": 1,
    "operator": 2,
    "admin": 3,
}


@dataclass(frozen=True, slots=True)
class AdminIdentity:
    actor_id: str
    role: AdminRole

    def has_role(self, required_role: AdminRole) -> bool:
        return ROLE_HIERARCHY.get(self.role, 0) >= ROLE_HIERARCHY.get(required_role, 0)


def _extract_token(x_admin_token: str | None, authorization: str | None) -> str | None:
    if x_admin_token and x_admin_token.strip():
        return x_admin_token.strip()
    if authorization:
        parts = authorization.strip().split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1]
    return None


async def get_admin_identity(
    request: Request,
    x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    x_admin_role: Annotated[str | None, Header(alias="X-Admin-Role")] = None,
    x_admin_actor_id: Annotated[str | None, Header(alias="X-Admin-Actor-Id")] = None,
) -> AdminIdentity:
    settings: Settings = getattr(request.app.state, "settings", Settings())
    expected_token_secret = settings.snapshot_admin_api_token or settings.snapshot_vss_api_token

    if expected_token_secret is not None:
        expected_token = expected_token_secret.get_secret_value()
        provided_token = _extract_token(x_admin_token, authorization)
        if not provided_token or not secrets.compare_digest(provided_token, expected_token):
            raise ApiError(
                status_code=401,
                reason="UNAUTHENTICATED",
                detail="관리자 인증 토큰이 없거나 올바르지 않습니다.",
                retryable=False,
            )

    normalized_role: AdminRole = "admin"
    if x_admin_role:
        cleaned_role = x_admin_role.strip().lower()
        if cleaned_role in ROLE_HIERARCHY:
            normalized_role = cleaned_role  # type: ignore[assignment]

    actor_id = (x_admin_actor_id or "admin").strip() or "admin"
    return AdminIdentity(actor_id=actor_id, role=normalized_role)


def require_admin_role(required_role: AdminRole):
    async def role_checker(
        identity: Annotated[AdminIdentity, Depends(get_admin_identity)],
    ) -> AdminIdentity:
        if not identity.has_role(required_role):
            raise ApiError(
                status_code=403,
                reason="FORBIDDEN",
                detail=f"해당 작업을 수행하기 위한 권한({required_role})이 부족합니다.",
                retryable=False,
            )
        return identity

    return role_checker
