"""Unit tests for Admin authentication and RBAC dependency."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

from backend.core.config import Settings
from backend.core.errors import ApiError
from backend.features.admin.auth import (
    AdminIdentity,
    _extract_token,
    get_admin_identity,
    require_admin_role,
)


def test_admin_identity_role_hierarchy() -> None:
    admin = AdminIdentity(actor_id="admin_user", role="admin")
    operator = AdminIdentity(actor_id="op_user", role="operator")
    viewer = AdminIdentity(actor_id="view_user", role="viewer")

    assert admin.has_role("viewer") is True
    assert admin.has_role("operator") is True
    assert admin.has_role("admin") is True

    assert operator.has_role("viewer") is True
    assert operator.has_role("operator") is True
    assert operator.has_role("admin") is False

    assert viewer.has_role("viewer") is True
    assert viewer.has_role("operator") is False
    assert viewer.has_role("admin") is False


def test_extract_token_variants() -> None:
    assert _extract_token("token123", None) == "token123"
    assert _extract_token(None, "Bearer token456") == "token456"
    assert _extract_token(None, "bearer token789") == "token789"
    assert _extract_token(None, "Basic dXNlcjpwYXNz") is None
    assert _extract_token("", "") is None
    assert _extract_token(None, None) is None


@pytest.mark.anyio
async def test_get_admin_identity_with_configured_token() -> None:
    settings = Settings(snapshot_admin_api_token=SecretStr("secret_admin_token_123"))
    request = MagicMock()
    request.app.state.settings = settings

    # Valid token via header
    identity = await get_admin_identity(
        request,
        x_admin_token="secret_admin_token_123",
        x_admin_role="operator",
        x_admin_actor_id="operator_bob",
    )
    assert identity.actor_id == "operator_bob"
    assert identity.role == "operator"

    # Valid token via Bearer Authorization
    identity_bearer = await get_admin_identity(
        request,
        authorization="Bearer secret_admin_token_123",
    )
    assert identity_bearer.actor_id == "admin"
    assert identity_bearer.role == "admin"

    # Invalid token -> 401 ApiError
    with pytest.raises(ApiError) as exc_info:
        await get_admin_identity(request, x_admin_token="wrong_token")
    assert exc_info.value.status_code == 401
    assert exc_info.value.reason == "UNAUTHENTICATED"

    # Missing token -> 401 ApiError
    with pytest.raises(ApiError) as exc_info:
        await get_admin_identity(request)
    assert exc_info.value.status_code == 401


@pytest.mark.anyio
async def test_get_admin_identity_without_configured_token() -> None:
    settings = Settings(snapshot_admin_api_token=None, snapshot_vss_api_token=None)
    request = MagicMock()
    request.app.state.settings = settings

    # Open in development
    identity = await get_admin_identity(request, x_admin_role="viewer")
    assert identity.role == "viewer"
    assert identity.actor_id == "admin"


@pytest.mark.anyio
async def test_require_admin_role_enforcement() -> None:
    admin = AdminIdentity(actor_id="admin_user", role="admin")
    viewer = AdminIdentity(actor_id="view_user", role="viewer")

    admin_checker = require_admin_role("admin")
    operator_checker = require_admin_role("operator")
    viewer_checker = require_admin_role("viewer")

    # Admin passes all
    assert await admin_checker(admin) == admin
    assert await operator_checker(admin) == admin
    assert await viewer_checker(admin) == admin

    # Viewer passes viewer only
    assert await viewer_checker(viewer) == viewer

    with pytest.raises(ApiError) as exc_info:
        await operator_checker(viewer)
    assert exc_info.value.status_code == 403
    assert exc_info.value.reason == "FORBIDDEN"

    with pytest.raises(ApiError) as exc_info:
        await admin_checker(viewer)
    assert exc_info.value.status_code == 403
    assert exc_info.value.reason == "FORBIDDEN"

