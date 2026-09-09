"""Integration tests for Admin Commit Materialization API (Phase 7B-3)."""

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
from backend.features.materialization.service import MaterializedTree
from backend.features.repository_collection.git_client import RepositoryGitClient
from backend.features.repository_collection.materializer import CollectedRevisionMaterializer
from backend.infrastructure.database.base import Base
from backend.infrastructure.database.models import (
    AuditLog,
    Repository,
    RepositoryCommit,
    Snapshot,
    TrackedBranch,
)

SERVICE_TOKEN = "service-token-with-enough-entropy"
IDENTITY_SECRET = "identity-secret-with-at-least-32-bytes"

SHA_VALID = "4444444444444444444444444444444444444444"
SHA_MISSING = "9999999999999999999999999999999999999999"


def _signed_request(
    client: TestClient,
    method: str,
    path: str,
    *,
    role: str,
    actor: str = "operator-kaypa",
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
    sig = hmac.new(IDENTITY_SECRET.encode(), canonical, hashlib.sha256).hexdigest()
    headers = {
        "Authorization": f"Bearer {SERVICE_TOKEN}",
        "X-Admin-Actor": actor,
        "X-Admin-Role": role,
        "X-Admin-Timestamp": timestamp,
        "X-Admin-Request-ID": request_id,
        "X-Admin-Content-SHA256": content_sha256,
        "X-Admin-Signature": sig,
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    return client.request(method, path, headers=headers, content=body if payload else None)


@pytest.fixture
def test_setup(tmp_path: Path):
    db_file = tmp_path / "test_admin_materialize.db"
    db_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    sync_url = f"sqlite:///{db_file.as_posix()}"

    sync_engine = create_engine(
        sync_url,
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    Base.metadata.create_all(sync_engine)

    repo_id = uuid4()
    tb_id = uuid4()

    with Session(sync_engine) as session:
        repo = Repository(
            repository_id=repo_id,
            canonical_name="h5vision/vision-core",
            display_name="Vision Core",
            provider="github",
            remote_url="https://github.com/h5vision/vision-core.git",
            default_branch_ref="refs/heads/main",
            active=True,
        )
        session.add(repo)

        tb = TrackedBranch(
            tracked_branch_id=tb_id,
            repository_id=repo_id,
            branch_ref="refs/heads/main",
            vss_project_id="vss-project-core",
            current_head_sha=SHA_VALID,
            tracked=True,
        )
        session.add(tb)

        commit = RepositoryCommit(
            repository_id=repo_id,
            commit_sha=SHA_VALID,
            tree_sha="a" * 40,
            author_name="Alice",
            authored_at=datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc),
            committed_at=datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc),
            object_verified_at=datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc),
            last_seen_at=datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc),
            subject="feat: core parser",
        )
        session.add(commit)
        session.commit()

    settings = Settings(
        database_url=db_url,
        snapshot_admin_service_token=SecretStr(SERVICE_TOKEN),
        snapshot_admin_identity_secret=SecretStr(IDENTITY_SECRET),
        snapshot_admin_require_hmac=True,
        snapshot_materialization_root=tmp_path / "materialized",
    )

    app = create_app(settings)

    mock_git = MagicMock(spec=RepositoryGitClient)
    mock_git.has_commit.return_value = True

    mock_materializer = MagicMock(spec=CollectedRevisionMaterializer)
    mock_materializer.materialize.return_value = MaterializedTree(
        project_root=tmp_path / "materialized" / tb_id.hex / "revisions" / SHA_VALID,
        locator=f"{tb_id.hex}/revisions/{SHA_VALID}",
        source_type="remote_clone",
    )

    with TestClient(app) as client:
        client.app.state.repository_git_client = mock_git
        client.app.state.collected_revision_materializer = mock_materializer
        yield client, repo_id, mock_git, mock_materializer, sync_engine


def test_materialize_commit_viewer_forbidden(test_setup):
    client, repo_id, _, _, _ = test_setup

    response = _signed_request(
        client,
        "POST",
        f"/v1/admin/repositories/{repo_id}/commits/{SHA_VALID}/materialize",
        role="viewer",
    )
    assert response.status_code == 403


def test_materialize_commit_not_found(test_setup):
    client, repo_id, _, _, _ = test_setup

    # Unknown commit
    response = _signed_request(
        client,
        "POST",
        f"/v1/admin/repositories/{repo_id}/commits/{SHA_MISSING}/materialize",
        role="operator",
    )
    assert response.status_code == 404
    assert response.json()["reason"] == "COMMIT_NOT_FOUND"


def test_materialize_commit_operator_success(test_setup):
    client, repo_id, _, mock_mat, sync_engine = test_setup

    response = _signed_request(
        client,
        "POST",
        f"/v1/admin/repositories/{repo_id}/commits/{SHA_VALID}/materialize",
        role="operator",
        actor="operator-kaypa",
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["repository_id"] == str(repo_id)
    assert payload["commit_sha"] == SHA_VALID
    assert payload["state"] == "materialized"
    assert payload["created"] is True
    assert payload["materialized_locator"] is not None
    assert payload["vss_project_id"] == "vss-project-core"

    # Verify DB Snapshot record
    with Session(sync_engine) as session:
        snap = session.query(Snapshot).filter_by(
            repository_id=repo_id,
            target_revision=SHA_VALID,
        ).first()
        assert snap is not None
        assert snap.state == "materialized"
        assert snap.materialized_locator == payload["materialized_locator"]


def test_materialize_commit_idempotent(test_setup):
    client, repo_id, _, mock_mat, _ = test_setup

    # First call -> creates
    res1 = _signed_request(
        client,
        "POST",
        f"/v1/admin/repositories/{repo_id}/commits/{SHA_VALID}/materialize",
        role="operator",
    )
    assert res1.status_code == 200
    p1 = res1.json()
    assert p1["created"] is True

    # Second call -> idempotent reuse
    res2 = _signed_request(
        client,
        "POST",
        f"/v1/admin/repositories/{repo_id}/commits/{SHA_VALID}/materialize",
        role="operator",
    )
    assert res2.status_code == 200
    p2 = res2.json()
    assert p2["created"] is False
    assert p2["snapshot_id"] == p1["snapshot_id"]
    assert p2["materialized_locator"] == p1["materialized_locator"]


def test_materialize_commit_records_audit(test_setup):
    client, repo_id, _, _, sync_engine = test_setup

    res = _signed_request(
        client,
        "POST",
        f"/v1/admin/repositories/{repo_id}/commits/{SHA_VALID}/materialize",
        role="operator",
        actor="operator-audit-kaypa",
    )
    assert res.status_code == 200

    with Session(sync_engine) as session:
        log = session.query(AuditLog).filter_by(action="materialize_commit").first()
        assert log is not None
        assert log.actor == "operator-audit-kaypa"
        assert log.target_type == "repository"
        assert log.target_id == str(repo_id)
        assert log.outcome == "succeeded"
        assert log.details["commit_sha"] == SHA_VALID
