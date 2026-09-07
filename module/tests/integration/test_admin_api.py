from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx2
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app import create_app
from backend.core.config import Settings
from backend.features.admin.auth import canonical_admin_request
from backend.features.indexing.index import IndexOutcome
from backend.features.indexing.retry import RetryOutcome
from backend.features.repository_collection.schemas import (
    RemoteBranchHead,
    RepositorySyncResult,
)
from backend.features.snapshots.schemas import SnapshotIndexResponse, SnapshotRetryResponse
from backend.infrastructure.database.base import Base
from backend.infrastructure.database.models import Snapshot, SnapshotAttempt

SERVICE_TOKEN = "service-token-with-enough-entropy"
IDENTITY_SECRET = "identity-secret-with-at-least-32-bytes"
COMMIT = "d858509f00c984e534922f98f2bf1776d3a2d870"


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
    return client.request(method, path, content=body, headers=headers)


def _create_database(path: Path):
    db_url = f"sqlite+aiosqlite:///{path}"
    engine = create_engine(
        f"sqlite:///{path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    Base.metadata.create_all(engine)
    return db_url, engine


def test_authenticated_admin_repository_branch_snapshot_and_audit_flow(tmp_path: Path) -> None:
    db_url, sync_engine = _create_database(tmp_path / "admin.db")

    def fake_vss(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/projects":
            return httpx2.Response(
                200,
                json={
                    "projects": [
                        {
                            "project_id": "vision--module",
                            "state": "done",
                            "commit": COMMIT,
                            "chunks": 42,
                            "indexed_at": "2026-09-01T00:00:00Z",
                            "project_root": "/private/vss/path",
                        }
                    ]
                },
            )
        return httpx2.Response(404, json={"detail": "not found"})

    settings = Settings(
        vision_environment="test",
        docs_enabled=False,
        database_url=SecretStr(db_url),
        snapshot_materialization_root=tmp_path / "materialized",
        snapshot_recovery_on_startup=False,
        snapshot_admin_service_token=SecretStr(SERVICE_TOKEN),
        snapshot_admin_identity_secret=SecretStr(IDENTITY_SECRET),
    )
    app = create_app(settings, vss_transport=httpx2.MockTransport(fake_vss))

    with TestClient(app) as client:
        unsigned = client.get(
            "/v1/admin/repositories",
            headers={"X-Admin-Actor": "forged", "X-Admin-Role": "admin"},
        )
        assert unsigned.status_code == 401

        viewer_create = _signed_request(
            client,
            "POST",
            "/v1/admin/repositories",
            role="viewer",
            payload={
                "canonical_name": "h5vision/vision",
                "display_name": "Vision",
                "provider": "github",
                "remote_url": "https://github.com/h5vision/vision.git",
                "default_branch_ref": "refs/heads/module",
            },
        )
        assert viewer_create.status_code == 403
        assert viewer_create.json()["reason"] == "ADMIN_PERMISSION_DENIED"

        created = _signed_request(
            client,
            "POST",
            "/v1/admin/repositories",
            role="admin",
            payload={
                "canonical_name": "h5vision/vision",
                "display_name": "Vision",
                "provider": "github",
                "remote_url": "https://github.com/h5vision/vision.git",
                "default_branch_ref": "refs/heads/module",
            },
        )
        assert created.status_code == 201
        assert created.headers["X-Request-ID"] == created.json()["request_id"]
        repository_id = created.json()["resource"]["repository_id"]

        updated_repository = _signed_request(
            client,
            "PATCH",
            f"/v1/admin/repositories/{repository_id}",
            role="admin",
            payload={"display_name": "Vision Updated"},
        )
        assert updated_repository.status_code == 200
        assert updated_repository.json()["resource"]["display_name"] == "Vision Updated"

        second_repository = _signed_request(
            client,
            "POST",
            "/v1/admin/repositories",
            role="admin",
            payload={
                "canonical_name": "h5vision/vision-second",
                "display_name": "Vision Second",
                "provider": "github",
                "remote_url": "https://github.com/h5vision/vision-second.git",
                "default_branch_ref": "refs/heads/main",
            },
        )
        assert second_repository.status_code == 201

        first_page = _signed_request(
            client,
            "GET",
            "/v1/admin/repositories?limit=1",
            role="viewer",
        )
        assert first_page.status_code == 200
        assert len(first_page.json()["items"]) == 1
        assert first_page.json()["next_cursor"] is not None
        second_page = _signed_request(
            client,
            "GET",
            (
                "/v1/admin/repositories?limit=1&cursor="
                f"{first_page.json()['next_cursor']}"
            ),
            role="viewer",
        )
        assert second_page.status_code == 200
        assert second_page.json()["items"][0]["repository_id"] != (
            first_page.json()["items"][0]["repository_id"]
        )

        collection_service = app.state.repository_collection_service
        collection_service._git_client = MagicMock()
        collection_service._git_client.list_remote_heads = MagicMock(
            return_value=[RemoteBranchHead(branch_ref="refs/heads/module", commit_sha=COMMIT)]
        )

        catalog = _signed_request(
            client,
            "GET",
            f"/v1/admin/repositories/{repository_id}/branches",
            role="viewer",
        )
        assert catalog.status_code == 200
        assert catalog.json()["branches"][0]["commit_sha"] == COMMIT

        tracked = _signed_request(
            client,
            "POST",
            "/v1/admin/tracked-branches",
            role="admin",
            payload={
                "repository_id": repository_id,
                "branch_ref": "refs/heads/module",
                "vss_project_id": "vision--module",
                "tracked": True,
            },
        )
        assert tracked.status_code == 201
        tracked_branch_id = tracked.json()["resource"]["tracked_branch_id"]

        updated_tracked = _signed_request(
            client,
            "PATCH",
            f"/v1/admin/tracked-branches/{tracked_branch_id}",
            role="admin",
            payload={"vss_project_id": "vision--module-updated"},
        )
        assert updated_tracked.status_code == 200
        assert updated_tracked.json()["resource"]["vss_project_id"] == (
            "vision--module-updated"
        )
        restored_tracked = _signed_request(
            client,
            "PATCH",
            f"/v1/admin/tracked-branches/{tracked_branch_id}",
            role="admin",
            payload={"vss_project_id": "vision--module"},
        )
        assert restored_tracked.status_code == 200

        created_binding = _signed_request(
            client,
            "POST",
            "/v1/admin/branch-bindings",
            role="admin",
            payload={
                "frontend_project_id": "h5vision/vision",
                "frontend_workspace_name": "vision",
                "repository_id": repository_id,
                "branch_ref": "refs/heads/module",
                "vss_project_id": "vision--frontend",
            },
        )
        assert created_binding.status_code == 201
        binding_id = created_binding.json()["resource"]["binding_id"]
        updated_binding = _signed_request(
            client,
            "PATCH",
            f"/v1/admin/branch-bindings/{binding_id}",
            role="admin",
            payload={"frontend_workspace_name": "vision-updated"},
        )
        assert updated_binding.status_code == 200
        assert updated_binding.json()["resource"]["frontend_workspace_name"] == (
            "vision-updated"
        )
        deactivated_binding = _signed_request(
            client,
            "DELETE",
            f"/v1/admin/branch-bindings/{binding_id}",
            role="admin",
        )
        assert deactivated_binding.status_code == 200
        assert deactivated_binding.json()["resource"]["active"] is False

        now = datetime.now(timezone.utc)
        collection_service.sync_repository = AsyncMock(
            return_value=RepositorySyncResult(
                ok=True,
                reason="COLLECTION_NO_CHANGES",
                detail="Remote HEAD is unchanged.",
                retryable=False,
                sync_run_id=uuid4(),
                repository_id=UUID(repository_id),
                trigger="manual",
                state="succeeded",
                started_at=now,
                finished_at=now,
                outcomes=[],
            )
        )
        synced = _signed_request(
            client,
            "POST",
            f"/v1/admin/repositories/{repository_id}/sync",
            role="operator",
        )
        assert synced.status_code == 200
        assert synced.json()["resource"]["state"] == "succeeded"

        collection_service.sync_repository = AsyncMock(
            return_value=RepositorySyncResult(
                ok=False,
                reason="REMOTE_UNAVAILABLE",
                detail="Remote fetch failed.",
                retryable=True,
                sync_run_id=uuid4(),
                repository_id=UUID(repository_id),
                trigger="manual",
                state="failed",
                started_at=now,
                finished_at=now,
                outcomes=[],
            )
        )
        failed_sync = _signed_request(
            client,
            "POST",
            f"/v1/admin/repositories/{repository_id}/sync",
            role="operator",
        )
        assert failed_sync.status_code == 503
        assert failed_sync.json()["ok"] is False
        assert failed_sync.json()["reason"] == "REMOTE_UNAVAILABLE"

        with Session(sync_engine) as session:
            snapshot = Snapshot(
                request_id=uuid4(),
                binding_id=None,
                tracked_branch_id=UUID(tracked_branch_id),
                frontend_project_id=None,
                repository_id=UUID(repository_id),
                branch_ref="refs/heads/module",
                vss_project_id="vision--module",
                base_revision=COMMIT,
                target_revision=COMMIT,
                source_type="remote_clone",
                state="failed",
                attempt_count=1,
                materialized_locator=str(tmp_path / "private" / COMMIT),
                vss_state="failed",
                vss_reason="VSS_INDEX_FAILED",
                vss_detail="Indexing failed.",
            )
            session.add(snapshot)
            session.flush()
            session.add(
                SnapshotAttempt(
                    snapshot_id=snapshot.snapshot_id,
                    request_id=uuid4(),
                    attempt_number=1,
                    vss_state="failed",
                    vss_reason="VSS_INDEX_FAILED",
                    vss_detail="Indexing failed.",
                    retryable=True,
                    vss_result_json={
                        "state": "failed",
                        "project_root": "/private/vss/path",
                        "token": "must-not-leak",
                    },
                )
            )
            session.commit()
            snapshot_id = str(snapshot.snapshot_id)

        snapshots = _signed_request(client, "GET", "/v1/admin/snapshots", role="viewer")
        assert snapshots.status_code == 200
        summary = snapshots.json()["items"][0]
        assert summary["binding_id"] is None
        assert summary["tracked_branch_id"] == tracked_branch_id
        assert summary["materialized_locator"] == f"revision:{COMMIT}"

        detail = _signed_request(
            client,
            "GET",
            f"/v1/admin/snapshots/{snapshot_id}",
            role="viewer",
        )
        assert detail.status_code == 200
        attempt = detail.json()["attempts"][0]
        assert attempt["vss_result_json"] == {"state": "failed"}

        viewer_index = _signed_request(
            client,
            "POST",
            f"/v1/admin/snapshots/{snapshot_id}/index",
            role="viewer",
        )
        assert viewer_index.status_code == 403
        assert viewer_index.json()["reason"] == "ADMIN_PERMISSION_DENIED"

        async def index_snapshot(
            requested_snapshot_id: UUID, *, request_id: UUID
        ) -> IndexOutcome:
            return IndexOutcome(
                status_code=202,
                body=SnapshotIndexResponse(
                    reason="VSS_INDEX_ACCEPTED",
                    detail="The materialized Snapshot index request was accepted.",
                    retryable=False,
                    request_id=request_id,
                    snapshot_id=requested_snapshot_id,
                    state="accepted",
                    attempt_count=1,
                ),
            )

        app.state.snapshot_index_service = MagicMock()
        app.state.snapshot_index_service.index = AsyncMock(side_effect=index_snapshot)
        indexed = _signed_request(
            client,
            "POST",
            f"/v1/admin/snapshots/{snapshot_id}/index",
            role="operator",
        )
        assert indexed.status_code == 202
        assert indexed.json()["reason"] == "VSS_INDEX_ACCEPTED"
        assert indexed.headers["X-Request-ID"] == indexed.json()["request_id"]

        async def retry_snapshot(
            requested_snapshot_id: UUID, *, request_id: UUID
        ) -> RetryOutcome:
            return RetryOutcome(
                status_code=202,
                body=SnapshotRetryResponse(
                    reason="VSS_INDEX_RETRY_ACCEPTED",
                    detail="The existing Snapshot retry was accepted.",
                    retryable=False,
                    request_id=request_id,
                    snapshot_id=requested_snapshot_id,
                    state="accepted",
                    attempt_count=2,
                ),
            )

        app.state.snapshot_retry_service = MagicMock()
        app.state.snapshot_retry_service.retry = AsyncMock(side_effect=retry_snapshot)
        retried = _signed_request(
            client,
            "POST",
            f"/v1/admin/snapshots/{snapshot_id}/retry",
            role="operator",
        )
        assert retried.status_code == 202
        assert retried.json()["reason"] == "VSS_INDEX_RETRY_ACCEPTED"
        assert retried.headers["X-Request-ID"] == retried.json()["request_id"]

        deactivated_tracked = _signed_request(
            client,
            "DELETE",
            f"/v1/admin/tracked-branches/{tracked_branch_id}",
            role="admin",
        )
        assert deactivated_tracked.status_code == 200
        assert deactivated_tracked.json()["resource"]["tracked"] is False

        projects = _signed_request(client, "GET", "/v1/admin/vss/projects", role="viewer")
        assert projects.status_code == 200
        assert projects.json()["items"][0] == {
            "project_id": "vision--module",
            "state": "done",
            "commit": COMMIT,
            "chunks": 42,
            "indexed_at": "2026-09-01T00:00:00Z",
        }

        deactivated_repository = _signed_request(
            client,
            "DELETE",
            f"/v1/admin/repositories/{repository_id}",
            role="admin",
        )
        assert deactivated_repository.status_code == 200
        assert deactivated_repository.json()["resource"]["active"] is False

        audit = _signed_request(client, "GET", "/v1/admin/audit-logs", role="admin")
        assert audit.status_code == 200
        actions = {item["action"]: item for item in audit.json()["items"]}
        assert actions["create_repository"]["actor"] == "kaypa"
        assert actions["register_tracked_branch"]["actor"] == "kaypa"
        assert actions["sync_repository"]["actor"] == "kaypa"
        assert actions["update_repository"]["actor"] == "kaypa"
        assert actions["update_tracked_branch"]["actor"] == "kaypa"
        assert actions["deactivate_tracked_branch"]["actor"] == "kaypa"
        assert actions["create_branch_binding"]["actor"] == "kaypa"
        assert actions["update_branch_binding"]["actor"] == "kaypa"
        assert actions["deactivate_branch_binding"]["actor"] == "kaypa"
        assert actions["index_snapshot"]["actor"] == "kaypa"
        assert actions["retry_snapshot"]["actor"] == "kaypa"
        assert actions["deactivate_repository"]["actor"] == "kaypa"
        assert actions["admin_request_denied"]["outcome"] == "denied"
        assert actions["admin_request_failed"]["reason"] == "HTTP_503"

    sync_engine.dispose()


def test_admin_runtime_models_reports_resident_ollama_models() -> None:
    def ollama(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/api/ps"
        return httpx2.Response(
            200,
            json={
                "models": [
                    {"name": "bge-m3:latest"},
                    {"name": "qwen3.8:27b"},
                ]
            },
        )

    settings = Settings(
        snapshot_admin_service_token=SecretStr(SERVICE_TOKEN),
        snapshot_admin_identity_secret=SecretStr(IDENTITY_SECRET),
        ollama_base_url="http://ollama.test:11434",
        snapshot_recovery_on_startup=False,
    )
    app = create_app(settings, ollama_transport=httpx2.MockTransport(ollama))

    with TestClient(app) as client:
        response = _signed_request(
            client,
            "GET",
            "/v1/admin/runtime/models",
            role="viewer",
        )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "available": True,
        "models": ["bge-m3:latest", "qwen3.8:27b"],
    }


def test_admin_runtime_models_stays_available_when_ollama_is_down() -> None:
    def unavailable(_request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("ollama is down")

    settings = Settings(
        snapshot_admin_service_token=SecretStr(SERVICE_TOKEN),
        snapshot_admin_identity_secret=SecretStr(IDENTITY_SECRET),
        ollama_base_url="http://ollama.test:11434",
        snapshot_recovery_on_startup=False,
    )
    app = create_app(settings, ollama_transport=httpx2.MockTransport(unavailable))

    with TestClient(app) as client:
        response = _signed_request(
            client,
            "GET",
            "/v1/admin/runtime/models",
            role="viewer",
        )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "available": False,
        "models": [],
    }
