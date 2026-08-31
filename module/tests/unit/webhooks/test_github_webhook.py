"""Unit and integration tests for GitHub Webhook (/postrecive) handling."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine

from backend.app import create_app
from backend.core.config import Settings
from backend.core.errors import ApiError
from backend.features.collection.service import SyncRunSummary
from backend.features.webhooks.schemas import GitHubWebhookRepo
from backend.features.webhooks.service import (
    find_matching_repository,
    verify_github_signature,
)
from backend.infrastructure.database.base import Base
from backend.infrastructure.database.models import Repository


def _compute_signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


# -----------------------------------------------------------------------------
# 1. Signature Verification Unit Tests
# -----------------------------------------------------------------------------


def test_verify_github_signature_success() -> None:
    secret = "my_webhook_secret_key"
    payload = b'{"ref": "refs/heads/main"}'
    sig = _compute_signature(secret, payload)

    # Should not raise
    verify_github_signature(payload_bytes=payload, signature_header=sig, secret=secret)


def test_verify_github_signature_mismatch() -> None:
    secret = "my_webhook_secret_key"
    payload = b'{"ref": "refs/heads/main"}'
    wrong_sig = "sha256=0000000000000000000000000000000000000000000000000000000000000000"

    with pytest.raises(ApiError) as exc_info:
        verify_github_signature(payload_bytes=payload, signature_header=wrong_sig, secret=secret)
    assert exc_info.value.status_code == 401
    assert exc_info.value.reason == "INVALID_WEBHOOK_SIGNATURE"


def test_verify_github_signature_missing_header() -> None:
    secret = "my_webhook_secret_key"
    payload = b'{"ref": "refs/heads/main"}'

    with pytest.raises(ApiError) as exc_info:
        verify_github_signature(payload_bytes=payload, signature_header=None, secret=secret)
    assert exc_info.value.status_code == 401
    assert exc_info.value.reason == "INVALID_WEBHOOK_SIGNATURE"


def test_verify_github_signature_no_secret_configured() -> None:
    payload = b'{"ref": "refs/heads/main"}'
    # Should pass without verification when secret is None
    verify_github_signature(payload_bytes=payload, signature_header=None, secret=None)


# -----------------------------------------------------------------------------
# 2. Repository Matching Tests
# -----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_find_matching_repository() -> None:
    session = AsyncMock()
    repo1 = Repository(
        repository_id=uuid4(),
        canonical_name="h5vision-vss_server",
        display_name="vss_server",
        provider="github",
        remote_url="https://github.com/h5vision/vss_server.git",
        default_branch_ref="refs/heads/main",
        active=True,
    )
    session.scalars.return_value = [repo1]

    # Match by clone_url without .git
    info1 = GitHubWebhookRepo(
        clone_url="https://github.com/h5vision/vss_server",
        html_url="https://github.com/h5vision/vss_server",
    )
    matched = await find_matching_repository(session, info1)
    assert matched is repo1

    # Match by name
    info2 = GitHubWebhookRepo(
        clone_url="https://other.com/repo",
        name="vss_server",
    )
    matched2 = await find_matching_repository(session, info2)
    assert matched2 is repo1

    # No match
    info3 = GitHubWebhookRepo(
        clone_url="https://github.com/unrelated/repo",
        full_name="unrelated/repo",
        name="repo",
    )
    matched3 = await find_matching_repository(session, info3)
    assert matched3 is None


# -----------------------------------------------------------------------------
# 3. HTTP End-to-End Webhook Endpoint Tests
# -----------------------------------------------------------------------------


def test_github_webhook_ping_and_push_flow(tmp_path: Path) -> None:
    db_path = tmp_path / "webhook_test.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    sync_engine = create_engine(
        f"sqlite:///{db_path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    Base.metadata.create_all(sync_engine)

    secret_key = "test_webhook_secret_999"
    settings = Settings(
        database_url=SecretStr(db_url),
        snapshot_materialization_root=tmp_path / "snapshots",
        snapshot_collection_root=tmp_path / "repositories",
        snapshot_webhook_secret=SecretStr(secret_key),
        snapshot_collection_sync_interval_seconds=0.0,
        snapshot_recovery_on_startup=False,
    )

    app = create_app(settings=settings)

    # Insert test repository into DB
    with sync_engine.connect() as conn:
        repo_id = uuid4()
        conn.exec_driver_sql(
            """
            INSERT INTO repositories (
                repository_id, canonical_name, display_name, provider,
                remote_url, default_branch_ref, active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            """,
            (
                str(repo_id),
                "h5vision-vss_server",
                "vss_server",
                "github",
                "https://github.com/h5vision/vss_server.git",
                "refs/heads/main",
                1,
            ),
        )
        conn.commit()

    with TestClient(app) as client:
        # Mock Collection Service
        mock_sync_summary = SyncRunSummary(
            sync_run_id=uuid4(),
            repository_id=repo_id,
            state="succeeded",
            reason=None,
            detail="동기화 성공",
            observed_branches=2,
            changed_branches=1,
            snapshots_created=1,
            snapshots_accepted=1,
            snapshot_failures=0,
        )
        app.state.collection_service.sync_repository = AsyncMock(return_value=mock_sync_summary)

        # A. Ping event test on /postrecive
        ping_payload = {"zen": "Mind your words.", "hook_id": 12345}
        ping_body = json.dumps(ping_payload).encode("utf-8")
        ping_sig = _compute_signature(secret_key, ping_body)

        ping_resp = client.post(
            "/postrecive",
            content=ping_body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "ping",
                "X-Hub-Signature-256": ping_sig,
            },
        )
        assert ping_resp.status_code == 200
        assert ping_resp.json()["reason"] == "PONG"
        assert ping_resp.json()["event"] == "ping"

        # B. Invalid signature test
        push_payload = {
            "ref": "refs/heads/main",
            "after": "97546fbcea6607a29ad0cc10246a7886bb44ceab",
            "repository": {
                "clone_url": "https://github.com/h5vision/vss_server.git",
                "full_name": "h5vision/vss_server",
            },
        }
        push_body = json.dumps(push_payload).encode("utf-8")
        bad_resp = client.post(
            "/postrecive",
            content=push_body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": "sha256=invalid_hash",
            },
        )
        assert bad_resp.status_code == 401
        assert bad_resp.json()["reason"] == "INVALID_WEBHOOK_SIGNATURE"

        # C. Push event test on /postrecive with valid signature
        good_sig = _compute_signature(secret_key, push_body)
        push_resp = client.post(
            "/postrecive",
            content=push_body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": good_sig,
            },
        )
        assert push_resp.status_code == 200
        push_data = push_resp.json()
        assert push_data["ok"] is True
        assert push_data["reason"] == "SYNC_COMPLETED"
        assert push_data["branch_ref"] == "refs/heads/main"
        assert push_data["after"] == "97546fbcea6607a29ad0cc10246a7886bb44ceab"
        assert push_data["summary"]["snapshots_accepted"] == 1

        app.state.collection_service.sync_repository.assert_awaited_once_with(
            repo_id, trigger="webhook"
        )

        # D. Test alias paths (/postreceive and /v1/webhooks/github)
        alias_resp = client.post(
            "/postreceive",
            content=ping_body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "ping",
                "X-Hub-Signature-256": ping_sig,
            },
        )
        assert alias_resp.status_code == 200
        assert alias_resp.json()["reason"] == "PONG"

        api_path_resp = client.post(
            "/v1/webhooks/github",
            content=ping_body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "ping",
                "X-Hub-Signature-256": ping_sig,
            },
        )
        assert api_path_resp.status_code == 200
        assert api_path_resp.json()["reason"] == "PONG"
