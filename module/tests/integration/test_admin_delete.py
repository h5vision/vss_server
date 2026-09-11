from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from uuid import UUID, uuid4

import httpx2
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from backend.app import create_app
from backend.core.config import Settings
from backend.features.admin.auth import canonical_admin_request
from backend.infrastructure.database.base import Base
from backend.infrastructure.database.models import (
    AuditLog,
    Repository,
    Snapshot,
    TrackedBranch,
)

SERVICE_TOKEN = "service-token-with-enough-entropy"
IDENTITY_SECRET = "identity-secret-with-at-least-32-bytes"
COMMIT = "d858509f00c984e534922f98f2bf1776d3a2d870"


def _signed_request(
    client: TestClient,
    method: str,
    path: str,
    *,
    role: str,
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
        actor="kaypa",
        role=role,
        timestamp=timestamp,
        request_id=request_id,
    )
    signature = hmac.new(IDENTITY_SECRET.encode(), canonical, hashlib.sha256).hexdigest()
    headers = {
        "Authorization": f"Bearer {SERVICE_TOKEN}",
        "X-Admin-Actor": "kaypa",
        "X-Admin-Role": role,
        "X-Admin-Timestamp": timestamp,
        "X-Admin-Request-ID": request_id,
        "X-Admin-Content-SHA256": content_sha256,
        "X-Admin-Signature": signature,
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    return client.request(method, path, content=body, headers=headers)


def _database(path: Path):
    db_url = f"sqlite+aiosqlite:///{path}"
    engine = create_engine(
        f"sqlite:///{path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    Base.metadata.create_all(engine)
    return db_url, engine


def _settings(db_url: str, tmp_path: Path) -> Settings:
    return Settings(
        vision_environment="test",
        docs_enabled=False,
        database_url=SecretStr(db_url),
        snapshot_materialization_root=tmp_path / "materialized",
        snapshot_repository_root=tmp_path / "repos",
        snapshot_recovery_on_startup=False,
        snapshot_admin_service_token=SecretStr(SERVICE_TOKEN),
        snapshot_admin_identity_secret=SecretStr(IDENTITY_SECRET),
    )


def _create_repository(client: TestClient) -> UUID:
    response = _signed_request(
        client,
        "POST",
        "/v1/admin/repositories",
        role="admin",
        payload={
            "canonical_name": "h5vision/delete-e2e",
            "display_name": "Delete E2E",
            "provider": "github",
            "remote_url": "https://github.com/h5vision/delete-e2e.git",
            "default_branch_ref": "refs/heads/main",
        },
    )
    assert response.status_code == 201
    return UUID(response.json()["resource"]["repository_id"])


def _seed_snapshot(engine, repository_id: UUID, *, state: str = "completed") -> None:
    tracked_branch_id = uuid4()
    snapshot_id = uuid4()
    with Session(engine) as session:
        session.add(
            TrackedBranch(
                tracked_branch_id=tracked_branch_id,
                repository_id=repository_id,
                branch_ref="refs/heads/main",
                vss_project_id="delete-e2e--main",
                current_head_sha=COMMIT,
                tracked=True,
            )
        )
        session.add(
            Snapshot(
                snapshot_id=snapshot_id,
                request_id=uuid4(),
                tracked_branch_id=tracked_branch_id,
                repository_id=repository_id,
                branch_ref="refs/heads/main",
                vss_project_id="delete-e2e--main",
                base_revision="0" * 40,
                target_revision=COMMIT,
                source_type="remote_clone",
                state=state,
            )
        )
        session.commit()


def test_admin_can_purge_registered_repository_and_module_history(tmp_path: Path) -> None:
    db_url, engine = _database(tmp_path / "purge.db")
    app = create_app(_settings(db_url, tmp_path))

    with TestClient(app) as client:
        repository_id = _create_repository(client)
        _seed_snapshot(engine, repository_id)

        wrong = _signed_request(
            client,
            "DELETE",
            f"/v1/admin/repositories/{repository_id}/purge?confirm=wrong",
            role="admin",
        )
        assert wrong.status_code == 409
        assert wrong.json()["reason"] == "REPOSITORY_PURGE_CONFIRMATION_REQUIRED"

        deleted = _signed_request(
            client,
            "DELETE",
            f"/v1/admin/repositories/{repository_id}/purge?confirm={repository_id}",
            role="admin",
        )
        assert deleted.status_code == 200
        assert deleted.json()["reason"] == "REPOSITORY_PURGED"
        assert deleted.json()["resource"]["deleted"] is True
        assert deleted.json()["resource"]["vss_project_ids"] == ["delete-e2e--main"]
        assert deleted.json()["resource"]["deleted_rows"]["repositories"] == 1

        missing = _signed_request(
            client,
            "GET",
            f"/v1/admin/repositories/{repository_id}",
            role="viewer",
        )
        assert missing.status_code == 404

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Repository)) == 0
        assert session.scalar(select(func.count()).select_from(TrackedBranch)) == 0
        assert session.scalar(select(func.count()).select_from(Snapshot)) == 0
        actions = set(session.scalars(select(AuditLog.action)))
        assert "purge_repository" in actions
    engine.dispose()


def test_repository_purge_refuses_active_index_history(tmp_path: Path) -> None:
    db_url, engine = _database(tmp_path / "busy.db")
    app = create_app(_settings(db_url, tmp_path))

    with TestClient(app) as client:
        repository_id = _create_repository(client)
        _seed_snapshot(engine, repository_id, state="accepted")

        response = _signed_request(
            client,
            "DELETE",
            f"/v1/admin/repositories/{repository_id}/purge?confirm={repository_id}",
            role="admin",
        )
        assert response.status_code == 409
        assert response.json()["reason"] == "REPOSITORY_PURGE_BUSY"

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Repository)) == 1
        assert session.scalar(select(func.count()).select_from(Snapshot)) == 1
    engine.dispose()


def test_admin_can_delete_selected_vss_vector_project(tmp_path: Path) -> None:
    db_url, engine = _database(tmp_path / "vector.db")
    present = True

    def fake_vss(request: httpx2.Request) -> httpx2.Response:
        nonlocal present
        if request.method == "GET" and request.url.path == "/projects":
            projects = []
            if present:
                projects.append(
                    {
                        "project_id": "delete-e2e--main",
                        "state": "done",
                        "commit": COMMIT,
                        "chunks": 17,
                        "indexed_at": "2026-09-08T00:00:00Z",
                    }
                )
            projects.append(
                {
                    "project_id": "delete-e2e--main--ast-v3-newer",
                    "state": "done",
                    "commit": "e" * 40,
                    "chunks": 23,
                    "indexed_at": "2026-09-11T00:00:00Z",
                }
            )
            return httpx2.Response(200, json={"projects": projects, "incomplete": []})
        if request.method == "GET" and request.url.path == "/index/status":
            return httpx2.Response(
                200,
                json={
                    "project_id": "delete-e2e--main",
                    "index_id": "delete-e2e--main",
                    "state": "done",
                    "index": {"commit": COMMIT, "chunks": 17},
                },
            )
        if request.method == "GET" and request.url.path == "/briefing/status":
            return httpx2.Response(200, json={"project_id": "delete-e2e--main", "state": "ready"})
        if request.method == "DELETE" and request.url.path == "/projects":
            assert request.url.params.get("project_id") == "delete-e2e--main"
            present = False
            return httpx2.Response(204)
        return httpx2.Response(404, json={"detail": "not found"})

    app = create_app(_settings(db_url, tmp_path), vss_transport=httpx2.MockTransport(fake_vss))
    path = "/v1/admin/vss/projects/delete-e2e--main?confirm=delete-e2e--main"

    with TestClient(app) as client:
        denied = _signed_request(client, "DELETE", path, role="operator")
        assert denied.status_code == 403

        wrong = _signed_request(
            client,
            "DELETE",
            "/v1/admin/vss/projects/delete-e2e--main?confirm=wrong",
            role="admin",
        )
        assert wrong.status_code == 409
        assert wrong.json()["reason"] == "VSS_PROJECT_DELETE_CONFIRMATION_REQUIRED"

        deleted = _signed_request(client, "DELETE", path, role="admin")
        assert deleted.status_code == 200
        assert deleted.json()["reason"] == "VSS_PROJECT_DELETED"
        assert deleted.json()["resource"]["project_id"] == "delete-e2e--main"
        assert deleted.json()["resource"]["chunks"] == 17
        assert deleted.json()["resource"]["deleted"] is True
        assert present is False

    with Session(engine) as session:
        actions = set(session.scalars(select(AuditLog.action)))
        assert "delete_vss_project" in actions
    engine.dispose()



def test_vector_delete_is_blocked_while_related_index_is_running(tmp_path: Path) -> None:
    db_url, engine = _database(tmp_path / "busy-index.db")
    delete_called = False

    def fake_vss(request: httpx2.Request) -> httpx2.Response:
        nonlocal delete_called
        if request.method == "GET" and request.url.path == "/projects":
            return httpx2.Response(
                200,
                json={
                    "projects": [
                        {
                            "project_id": "delete-e2e--main",
                            "state": "done",
                            "commit": COMMIT,
                            "chunks": 17,
                        }
                    ],
                    "incomplete": [],
                },
            )
        if request.method == "GET" and request.url.path == "/index/status":
            return httpx2.Response(
                200,
                json={
                    "project_id": "delete-e2e--main",
                    "index_id": "delete-e2e--main--ast-v3-newer",
                    "state": "running",
                    "processed": 3,
                    "total": 20,
                },
            )
        if request.method == "GET" and request.url.path == "/briefing/status":
            return httpx2.Response(200, json={"project_id": "delete-e2e--main", "state": "ready"})
        if request.method == "DELETE" and request.url.path == "/projects":
            delete_called = True
            return httpx2.Response(204)
        return httpx2.Response(404, json={"detail": "not found"})

    app = create_app(_settings(db_url, tmp_path), vss_transport=httpx2.MockTransport(fake_vss))
    with TestClient(app) as client:
        response = _signed_request(
            client,
            "DELETE",
            "/v1/admin/vss/projects/delete-e2e--main?confirm=delete-e2e--main",
            role="admin",
        )

    assert response.status_code == 409
    assert response.json()["reason"] == "VSS_PROJECT_DELETE_BUSY"
    assert delete_called is False
    engine.dispose()


def test_vector_delete_is_blocked_while_briefing_is_running(tmp_path: Path) -> None:
    db_url, engine = _database(tmp_path / "busy-briefing.db")
    delete_called = False

    def fake_vss(request: httpx2.Request) -> httpx2.Response:
        nonlocal delete_called
        if request.method == "GET" and request.url.path == "/projects":
            return httpx2.Response(
                200,
                json={
                    "projects": [
                        {
                            "project_id": "delete-e2e--main",
                            "state": "done",
                            "commit": COMMIT,
                            "chunks": 17,
                        }
                    ],
                    "incomplete": [],
                },
            )
        if request.method == "GET" and request.url.path == "/index/status":
            return httpx2.Response(
                200,
                json={
                    "project_id": "delete-e2e--main",
                    "index_id": "delete-e2e--main",
                    "state": "done",
                    "index": {"commit": COMMIT, "chunks": 17},
                },
            )
        if request.method == "GET" and request.url.path == "/briefing/status":
            return httpx2.Response(
                200,
                json={"project_id": "delete-e2e--main", "state": "running"},
            )
        if request.method == "DELETE" and request.url.path == "/projects":
            delete_called = True
            return httpx2.Response(204)
        return httpx2.Response(404, json={"detail": "not found"})

    app = create_app(_settings(db_url, tmp_path), vss_transport=httpx2.MockTransport(fake_vss))
    with TestClient(app) as client:
        response = _signed_request(
            client,
            "DELETE",
            "/v1/admin/vss/projects/delete-e2e--main?confirm=delete-e2e--main",
            role="admin",
        )

    assert response.status_code == 409
    assert response.json()["reason"] == "VSS_PROJECT_DELETE_BUSY"
    assert delete_called is False
    engine.dispose()

def test_vector_delete_reports_missing_vss_maintenance_contract(tmp_path: Path) -> None:
    db_url, engine = _database(tmp_path / "unsupported.db")

    def fake_vss(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET" and request.url.path == "/projects":
            return httpx2.Response(
                200,
                json={
                    "projects": [
                        {
                            "project_id": "delete-e2e--main",
                            "state": "done",
                            "commit": COMMIT,
                            "chunks": 17,
                        }
                    ],
                    "incomplete": [],
                },
            )
        if request.method == "GET" and request.url.path == "/index/status":
            return httpx2.Response(
                200,
                json={
                    "project_id": "delete-e2e--main",
                    "index_id": "delete-e2e--main",
                    "state": "done",
                    "index": {"commit": COMMIT, "chunks": 17},
                },
            )
        if request.method == "GET" and request.url.path == "/briefing/status":
            return httpx2.Response(200, json={"project_id": "delete-e2e--main", "state": "ready"})
        if request.method == "DELETE" and request.url.path == "/projects":
            return httpx2.Response(404, json={"detail": "not found"})
        return httpx2.Response(404, json={"detail": "not found"})

    app = create_app(_settings(db_url, tmp_path), vss_transport=httpx2.MockTransport(fake_vss))
    with TestClient(app) as client:
        response = _signed_request(
            client,
            "DELETE",
            "/v1/admin/vss/projects/delete-e2e--main?confirm=delete-e2e--main",
            role="admin",
        )
        assert response.status_code == 501
        assert response.json()["reason"] == "VSS_PROJECT_DELETE_UNSUPPORTED"

    engine.dispose()
