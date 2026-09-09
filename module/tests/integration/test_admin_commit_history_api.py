"""Integration tests for Admin Commit History and Compare APIs."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app import create_app
from backend.core.config import Settings
from backend.features.admin.auth import canonical_admin_request
from backend.features.repository_collection.git_client import (
    GitCompareFileChange,
    GitCompareResult,
    RepositoryGitClient,
)
from backend.infrastructure.database.base import Base
from backend.infrastructure.database.models import (
    Repository,
    RepositoryCommit,
    RepositoryCommitParent,
    Snapshot,
    TrackedBranch,
)

SERVICE_TOKEN = "service-token-with-enough-entropy"
IDENTITY_SECRET = "identity-secret-with-at-least-32-bytes"

SHA_ROOT = "1111111111111111111111111111111111111111"
SHA_A = "2222222222222222222222222222222222222222"
SHA_B = "3333333333333333333333333333333333333333"


def _signed_request(
    client: TestClient,
    method: str,
    path: str,
    *,
    role: str,
    actor: str = "kaypa",
    payload: dict | None = None,
):
    body = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    request_id = str(uuid4())
    content_sha256 = hashlib.sha256(body).hexdigest()
    canonical = canonical_admin_request(
        method=method,
        path_with_query=path,
        content_sha256=content_sha256,
        actor=actor,
        role=role,
        timestamp=timestamp,
        request_id=request_id,
    )
    signature = hmac.new(IDENTITY_SECRET.encode(), canonical, hashlib.sha256).hexdigest()
    headers = {
        "Authorization": f"Bearer {SERVICE_TOKEN}",
        "X-Admin-Actor": actor,
        "X-Admin-Role": role,
        "X-Admin-Timestamp": timestamp,
        "X-Admin-Request-ID": request_id,
        "X-Admin-Content-SHA256": content_sha256,
        "X-Admin-Signature": signature,
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    return client.request(method, path, headers=headers, content=body if payload else None)


@pytest.fixture
def test_setup(tmp_path: Path):
    db_path = tmp_path / "admin_commit_test.db"
    database_url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    sync_database_url = f"sqlite:///{db_path.as_posix()}"

    sync_engine = create_engine(
        sync_database_url,
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    Base.metadata.create_all(sync_engine)

    repo_id = uuid4()
    with Session(sync_engine) as session:
        repo = Repository(
            repository_id=repo_id,
            canonical_name="test-repo",
            display_name="test-repo",
            provider="github",
            remote_url="https://github.com/example/test-repo.git",
            default_branch_ref="refs/heads/main",
            active=True,
        )
        session.add(repo)

        branch = TrackedBranch(
            tracked_branch_id=uuid4(),
            repository_id=repo_id,
            branch_ref="refs/heads/main",
            tracked=True,
            vss_project_id="test-proj",
        )
        session.add(branch)

        # Root commit
        c_root = RepositoryCommit(
            repository_id=repo_id,
            commit_sha=SHA_ROOT,
            tree_sha="a" * 40,
            author_name="Dev Root",
            authored_at=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
            committed_at=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
            subject="root commit",
            object_verified_at=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
            last_seen_at=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
        )
        session.add(c_root)
        session.flush()

        # Commit A (child of root) - Materialized Snapshot
        c_a = RepositoryCommit(
            repository_id=repo_id,
            commit_sha=SHA_A,
            tree_sha="b" * 40,
            author_name="Dev A",
            authored_at=datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc),
            committed_at=datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc),
            subject="commit a with snapshot",
            object_verified_at=datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc),
            last_seen_at=datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc),
        )
        session.add(c_a)
        session.flush()

        p_a = RepositoryCommitParent(
            repository_commit_id=c_a.repository_commit_id,
            parent_sha=SHA_ROOT,
            parent_order=0,
            parent_commit_id=c_root.repository_commit_id,
        )
        session.add(p_a)

        snap_a = Snapshot(
            snapshot_id=uuid4(),
            request_id=uuid4(),
            repository_id=repo_id,
            tracked_branch_id=branch.tracked_branch_id,
            branch_ref="refs/heads/main",
            vss_project_id="test-proj",
            base_revision=SHA_ROOT,
            target_revision=SHA_A,
            state="completed",
            vss_state="done",
            materialized_locator="/srv/vss-snapshots/test",
            created_at=datetime(2026, 9, 1, 11, 5, tzinfo=timezone.utc),
        )
        session.add(snap_a)

        # Commit B (child of A) - Git only (no snapshot)
        c_b = RepositoryCommit(
            repository_id=repo_id,
            commit_sha=SHA_B,
            tree_sha="c" * 40,
            author_name="Dev B",
            authored_at=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            committed_at=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            subject="commit b git only",
            object_verified_at=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            last_seen_at=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        )
        session.add(c_b)
        session.flush()

        p_b = RepositoryCommitParent(
            repository_commit_id=c_b.repository_commit_id,
            parent_sha=SHA_A,
            parent_order=0,
            parent_commit_id=c_a.repository_commit_id,
        )
        session.add(p_b)

        session.commit()

    settings = Settings(
        database_url=database_url,
        snapshot_admin_service_token=SecretStr(SERVICE_TOKEN),
        snapshot_admin_identity_secret=SecretStr(IDENTITY_SECRET),
        snapshot_admin_require_hmac=True,
        snapshot_materialization_root=tmp_path / "snapshots",
    )

    mock_git_client = MagicMock(spec=RepositoryGitClient)
    mock_git_client.compare_revisions.return_value = GitCompareResult(
        base_revision=SHA_ROOT,
        target_revision=SHA_B,
        merge_base_revision=SHA_ROOT,
        ahead_count=2,
        behind_count=0,
        files_changed=2,
        additions=10,
        deletions=2,
        changes=[
            GitCompareFileChange(path="file1.txt", change_type="modified"),
            GitCompareFileChange(path="file2.txt", change_type="added"),
        ],
    )

    app = create_app(settings=settings)
    app.state.repository_git_client = mock_git_client

    with TestClient(app) as client:
        client.app.state.repository_git_client = mock_git_client
        yield client, repo_id, mock_git_client


def test_list_commits_and_status(test_setup):
    client, repo_id, _ = test_setup

    response = _signed_request(
        client,
        "GET",
        f"/v1/admin/repositories/{repo_id}/commits?limit=10",
        role="viewer",
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert len(payload["items"]) == 3

    items = {it["commit_sha"]: it for it in payload["items"]}

    # Commit B is Git only
    assert items[SHA_B]["status"] == "git_only"
    assert items[SHA_B]["parent_shas"] == [SHA_A]
    assert items[SHA_B]["subject"] == "commit b git only"

    # Commit A is VSS indexed
    assert items[SHA_A]["status"] == "vss_indexed"
    assert items[SHA_A]["parent_shas"] == [SHA_ROOT]
    assert items[SHA_A]["snapshot_id"] is not None

    # Root commit
    assert items[SHA_ROOT]["status"] == "git_only"
    assert items[SHA_ROOT]["parent_shas"] == []


def test_get_commit_detail(test_setup):
    client, repo_id, _ = test_setup

    response = _signed_request(
        client,
        "GET",
        f"/v1/admin/repositories/{repo_id}/commits/{SHA_A}",
        role="viewer",
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    commit = payload["commit"]
    assert commit["commit_sha"] == SHA_A
    assert commit["status"] == "vss_indexed"
    assert commit["parent_shas"] == [SHA_ROOT]


def test_get_commit_detail_not_found(test_setup):
    client, repo_id, _ = test_setup
    missing_sha = "9" * 40

    response = _signed_request(
        client,
        "GET",
        f"/v1/admin/repositories/{repo_id}/commits/{missing_sha}",
        role="viewer",
    )
    assert response.status_code == 404
    assert response.json()["reason"] == "COMMIT_NOT_FOUND"


def test_compare_commits_operator(test_setup):
    client, repo_id, mock_git = test_setup

    response = _signed_request(
        client,
        "GET",
        f"/v1/admin/repositories/{repo_id}/compare?base_revision={SHA_ROOT}&target_revision={SHA_B}",
        role="operator",
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["base_revision"] == SHA_ROOT
    assert payload["target_revision"] == SHA_B
    assert payload["files_changed"] == 2
    assert payload["additions"] == 10
    assert payload["deletions"] == 2
    assert payload["base_status"] == "git_only"
    assert payload["target_status"] == "git_only"
    assert len(payload["changes"]) == 2

    mock_git.compare_revisions.assert_called_once_with(
        repository_id=repo_id,
        base_revision=SHA_ROOT,
        target_revision=SHA_B,
    )


def test_compare_commits_viewer_forbidden(test_setup):
    client, repo_id, _ = test_setup

    response = _signed_request(
        client,
        "GET",
        f"/v1/admin/repositories/{repo_id}/compare?base_revision={SHA_ROOT}&target_revision={SHA_B}",
        role="viewer",  # Viewer cannot run compare (operator required)
    )
    assert response.status_code == 403


def test_list_commits_pagination(test_setup):
    client, repo_id, _ = test_setup

    # First page with limit=1
    res1 = _signed_request(
        client,
        "GET",
        f"/v1/admin/repositories/{repo_id}/commits?limit=1",
        role="viewer",
    )
    assert res1.status_code == 200
    p1 = res1.json()
    assert len(p1["items"]) == 1
    assert p1["items"][0]["commit_sha"] == SHA_B  # Most recent commit
    assert p1["next_cursor"] is not None

    # Second page using next_cursor
    res2 = _signed_request(
        client,
        "GET",
        f"/v1/admin/repositories/{repo_id}/commits?limit=1&cursor={p1['next_cursor']}",
        role="viewer",
    )
    assert res2.status_code == 200
    p2 = res2.json()
    assert len(p2["items"]) == 1
    assert p2["items"][0]["commit_sha"] == SHA_A


def test_list_commits_filter_status(test_setup):
    client, repo_id, _ = test_setup

    response = _signed_request(
        client,
        "GET",
        f"/v1/admin/repositories/{repo_id}/commits?status=vss_indexed",
        role="viewer",
    )
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["items"]) == 1
    assert payload["items"][0]["commit_sha"] == SHA_A
    assert payload["items"][0]["status"] == "vss_indexed"


def test_compare_commits_records_audit(test_setup):
    client, repo_id, _ = test_setup

    # Perform compare as operator
    res = _signed_request(
        client,
        "GET",
        f"/v1/admin/repositories/{repo_id}/compare?base_revision={SHA_ROOT}&target_revision={SHA_B}",
        role="operator",
        actor="operator-kaypa",
    )
    assert res.status_code == 200

    # Query audit logs as admin
    audit_res = _signed_request(
        client,
        "GET",
        "/v1/admin/audit-logs?limit=10",
        role="admin",
    )
    assert audit_res.status_code == 200
    logs = audit_res.json()["items"]
    compare_logs = [lg for lg in logs if lg["action"] == "compare_commits"]
    assert len(compare_logs) >= 1
    lg = compare_logs[0]
    assert lg["actor"] == "operator-kaypa"
    assert lg["target_type"] == "repository"
    assert lg["target_id"] == str(repo_id)
    assert lg["outcome"] == "succeeded"
    assert lg["details"]["base_revision"] == SHA_ROOT
    assert lg["details"]["target_revision"] == SHA_B
