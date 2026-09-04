from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError

from backend.core.config import Settings
from backend.core.errors import ApiError
from backend.features.admin.auth import (
    AdminIdentity,
    canonical_admin_request,
    require_role,
    verify_admin_request,
)


def _settings() -> Settings:
    return Settings(
        vision_environment="test",
        snapshot_admin_service_token=SecretStr("service-token-with-enough-entropy"),
        snapshot_admin_identity_secret=SecretStr("identity-secret-with-at-least-32-bytes"),
    )


def _headers(
    *,
    method: str = "POST",
    path_with_query: str = "/v1/admin/repositories?active=true",
    body: bytes = b'{"display_name":"Vision"}',
    actor: str = "kaypa",
    role: str = "admin",
    timestamp: int = 1_788_189_600,
    request_id: str = "44444444-4444-4444-8444-444444444444",
) -> dict[str, str]:
    content_sha256 = hashlib.sha256(body).hexdigest()
    canonical = canonical_admin_request(
        method=method,
        path_with_query=path_with_query,
        content_sha256=content_sha256,
        actor=actor,
        role=role,
        timestamp=str(timestamp),
        request_id=request_id,
    )
    signature = hmac.new(
        b"identity-secret-with-at-least-32-bytes",
        canonical,
        hashlib.sha256,
    ).hexdigest()
    return {
        "authorization": "Bearer service-token-with-enough-entropy",
        "x-admin-actor": actor,
        "x-admin-role": role,
        "x-admin-timestamp": str(timestamp),
        "x-admin-request-id": request_id,
        "x-admin-content-sha256": content_sha256,
        "x-admin-signature": signature,
    }


def test_admin_request_signature_binds_identity_path_query_and_body() -> None:
    now = datetime.fromtimestamp(1_788_189_600, tz=timezone.utc)
    identity = verify_admin_request(
        settings=_settings(),
        method="POST",
        path_with_query="/v1/admin/repositories?active=true",
        body=b'{"display_name":"Vision"}',
        headers=_headers(),
        now=now,
    )

    assert identity == AdminIdentity(
        actor_id="kaypa",
        role="admin",
        request_id=UUID("44444444-4444-4444-8444-444444444444"),
    )

    with pytest.raises(ApiError) as captured:
        verify_admin_request(
            settings=_settings(),
            method="POST",
            path_with_query="/v1/admin/repositories?active=false",
            body=b'{"display_name":"Vision"}',
            headers=_headers(),
            now=now,
        )
    assert captured.value.status_code == 401
    assert captured.value.reason == "ADMIN_AUTHENTICATION_REQUIRED"


def test_admin_authentication_is_fail_closed_when_secrets_are_missing() -> None:
    with pytest.raises(ApiError) as captured:
        verify_admin_request(
            settings=Settings(vision_environment="test"),
            method="GET",
            path_with_query="/v1/admin/repositories",
            body=b"",
            headers={
                "x-admin-actor": "forged-admin",
                "x-admin-role": "admin",
            },
            now=datetime.now(timezone.utc),
        )

    assert captured.value.status_code == 503
    assert captured.value.reason == "ADMIN_AUTH_NOT_CONFIGURED"


def test_admin_service_and_identity_secrets_must_be_distinct() -> None:
    duplicated = "same-secret-value-with-at-least-32-bytes"

    with pytest.raises(ValidationError):
        Settings(
            snapshot_admin_service_token=duplicated,
            snapshot_admin_identity_secret=duplicated,
        )


@pytest.mark.parametrize(
    ("role", "required", "allowed"),
    [
        ("viewer", "viewer", True),
        ("viewer", "operator", False),
        ("operator", "viewer", True),
        ("operator", "operator", True),
        ("operator", "admin", False),
        ("admin", "viewer", True),
        ("admin", "admin", True),
    ],
)
def test_admin_role_hierarchy(role: str, required: str, allowed: bool) -> None:
    identity = AdminIdentity(
        actor_id="user",
        role=role,
        request_id=UUID("44444444-4444-4444-8444-444444444444"),
    )

    if allowed:
        assert require_role(identity, required) is identity
    else:
        with pytest.raises(ApiError) as captured:
            require_role(identity, required)
        assert captured.value.status_code == 403
        assert captured.value.reason == "ADMIN_PERMISSION_DENIED"


def test_admin_request_rejects_stale_timestamp_and_body_hash_spoofing() -> None:
    now = datetime.fromtimestamp(1_788_189_700, tz=timezone.utc)

    with pytest.raises(ApiError) as stale:
        verify_admin_request(
            settings=_settings(),
            method="POST",
            path_with_query="/v1/admin/repositories?active=true",
            body=b'{"display_name":"Vision"}',
            headers=_headers(),
            now=now,
        )
    assert stale.value.reason == "ADMIN_AUTHENTICATION_REQUIRED"

    tampered_headers = _headers(timestamp=1_788_189_700)
    with pytest.raises(ApiError) as tampered:
        verify_admin_request(
            settings=_settings(),
            method="POST",
            path_with_query="/v1/admin/repositories?active=true",
            body=b'{"display_name":"Changed"}',
            headers=tampered_headers,
            now=now,
        )
    assert tampered.value.reason == "ADMIN_AUTHENTICATION_REQUIRED"
