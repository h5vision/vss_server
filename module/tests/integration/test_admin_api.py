"""Integration tests for the authenticated Admin REST API."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx2
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine

from backend.app import create_app
from backend.core.config import Settings
from backend.infrastructure.database.base import Base


def test_admin_api_end_to_end_flow(tmp_path: Path) -> None:
    commit_sha = "d858509f00c984e534922f98f2bf1776d3a2d870"

    db_path = tmp_path / "admin_test.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    sync_engine = create_engine(
        f"sqlite:///{db_path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    Base.metadata.create_all(sync_engine)

    admin_token = "test_admin_secret_token_999"
    settings = Settings(
        database_url=SecretStr(db_url),
        snapshot_materialization_root=tmp_path / "snapshots",
        snapshot_collection_root=tmp_path / "repositories",
        snapshot_admin_api_token=SecretStr(admin_token),
        snapshot_collection_sync_interval_seconds=0.0,
        snapshot_recovery_on_startup=False,
    )

    vss_calls = []

    def mock_vss_handler(request: httpx2.Request) -> httpx2.Response:
        url_str = str(request.url)
        if request.method == "POST" and url_str.endswith("/index"):
            body = json.loads(request.content.decode("utf-8"))
            vss_calls.append(body)
            return httpx2.Response(
                202,
                json={
                    "accepted": True,
                    "project_id": body.get("project_id", "prj_test"),
                    "state": "running",
                },
            )
        if request.method == "GET" and url_str.endswith("/projects"):
            return httpx2.Response(
                200,
                json={
                    "projects": [
                        {
                            "project_id": "vss_proj_1",
                            "state": "done",
                            "commit": commit_sha,
                            "chunks": 12,
                            "indexed_at": "2026-08-31T00:00:00Z",
                        }
                    ]
                },
            )
        return httpx2.Response(200, json={"status": "ok"})

    transport = httpx2.MockTransport(mock_vss_handler)
    app = create_app(settings=settings, vss_transport=transport)

    with TestClient(app) as client:
        # Mock Git Collection client methods for isolated predictable execution
        git_mock = MagicMock()
        git_mock.remote_heads.return_value = {"refs/heads/main": commit_sha}
        git_mock.ensure_mirror.return_value = None
        git_mock.is_ancestor.return_value = False
        git_mock.checkout_tree.side_effect = lambda mdir, rev, dest: (
            dest.mkdir(parents=True, exist_ok=True)
            or (dest / "app.py").write_text("print('ready')", encoding="utf-8")
        )
        app.state.collection_service._git = git_mock
        app.state.collection_service._materializer._git = git_mock
        app.state.collection_service._materializer._attest = MagicMock()

        # 1. Test Authentication & RBAC Enforcement
        unauth_resp = client.get("/v1/admin/repositories")
        assert unauth_resp.status_code == 401
        assert unauth_resp.json()["reason"] == "UNAUTHENTICATED"

        wrong_token_resp = client.get(
            "/v1/admin/repositories",
            headers={"X-Admin-Token": "bad_token"},
        )
        assert wrong_token_resp.status_code == 401

        # Viewer trying to perform admin mutation -> 403
        forbidden_resp = client.post(
            "/v1/admin/repositories",
            headers={
                "X-Admin-Token": admin_token,
                "X-Admin-Role": "viewer",
            },
            json={
                "canonical_name": "repo/sample",
                "display_name": "Sample Repo",
                "provider": "github",
                "remote_url": "https://github.com/example/sample.git",
                "default_branch_ref": "refs/heads/main",
            },
        )
        assert forbidden_resp.status_code == 403
        assert forbidden_resp.json()["reason"] == "FORBIDDEN"

        # 2. Create Repository as Admin
        admin_headers = {
            "X-Admin-Token": admin_token,
            "X-Admin-Role": "admin",
            "X-Admin-Actor-Id": "super_admin",
        }
        create_repo_resp = client.post(
            "/v1/admin/repositories",
            headers=admin_headers,
            json={
                "canonical_name": "github.com/sample/core",
                "display_name": "Core Service",
                "provider": "github",
                "remote_url": "https://github.com/sample/core.git",
                "default_branch_ref": "refs/heads/main",
                "active": True,
            },
        )
        assert create_repo_resp.status_code == 201
        repo_data = create_repo_resp.json()["resource"]
        repo_id = repo_data["repository_id"]
        assert repo_data["canonical_name"] == "github.com/sample/core"

        # 3. List & Get Repositories
        list_repo_resp = client.get("/v1/admin/repositories", headers=admin_headers)
        assert list_repo_resp.status_code == 200
        assert len(list_repo_resp.json()["items"]) == 1

        get_repo_resp = client.get(f"/v1/admin/repositories/{repo_id}", headers=admin_headers)
        assert get_repo_resp.status_code == 200
        assert get_repo_resp.json()["display_name"] == "Core Service"

        # 4. Patch Repository
        patch_repo_resp = client.patch(
            f"/v1/admin/repositories/{repo_id}",
            headers=admin_headers,
            json={"display_name": "Core Service Renamed"},
        )
        assert patch_repo_resp.status_code == 200
        assert patch_repo_resp.json()["resource"]["display_name"] == "Core Service Renamed"

        # 5. Remote Branch Catalog Query
        catalog_resp = client.get(
            f"/v1/admin/repositories/{repo_id}/branches",
            headers=admin_headers,
        )
        assert catalog_resp.status_code == 200
        catalog_items = catalog_resp.json()["branches"]
        assert any(b["branch_ref"] == "refs/heads/main" for b in catalog_items)

        # 6. Create Tracked Branch
        create_branch_resp = client.post(
            "/v1/admin/tracked-branches",
            headers=admin_headers,
            json={
                "repository_id": repo_id,
                "branch_ref": "refs/heads/main",
                "vss_project_id": "vss_core_main",
                "tracked": True,
            },
        )
        assert create_branch_resp.status_code == 201
        branch_data = create_branch_resp.json()["resource"]
        tracked_branch_id = branch_data["tracked_branch_id"]
        assert branch_data["vss_project_id"] == "vss_core_main"

        # 7. List Tracked Branches
        list_branches_resp = client.get(
            f"/v1/admin/tracked-branches?repository_id={repo_id}",
            headers=admin_headers,
        )
        assert list_branches_resp.status_code == 200
        assert len(list_branches_resp.json()["items"]) == 1

        # 8. Manual Sync Trigger (Operator role)
        op_headers = {
            "X-Admin-Token": admin_token,
            "X-Admin-Role": "operator",
            "X-Admin-Actor-Id": "operator_alice",
        }
        sync_resp = client.post(
            f"/v1/admin/repositories/{repo_id}/sync",
            headers=op_headers,
        )
        assert sync_resp.status_code == 200
        sync_result = sync_resp.json()["resource"]
        assert sync_result["changed_branches"] == 1
        assert sync_result["snapshots_accepted"] == 1
        assert len(vss_calls) == 1

        # 9. Sync Runs History
        sync_runs_resp = client.get(
            f"/v1/admin/repositories/{repo_id}/sync-runs",
            headers=admin_headers,
        )
        assert sync_runs_resp.status_code == 200
        assert len(sync_runs_resp.json()["items"]) >= 1

        # 10. Tracked Branch History
        branch_hist_resp = client.get(
            f"/v1/admin/tracked-branches/{tracked_branch_id}/history",
            headers=admin_headers,
        )
        assert branch_hist_resp.status_code == 200
        assert len(branch_hist_resp.json()["items"]) == 1
        assert branch_hist_resp.json()["items"][0]["observed_head_sha"] == commit_sha

        # 11. Branch Bindings (Frontend Legacy) CRUD
        create_binding_resp = client.post(
            "/v1/admin/branch-bindings",
            headers=admin_headers,
            json={
                "frontend_project_id": "h5vision/core",
                "frontend_workspace_name": "core",
                "repository_id": repo_id,
                "branch_ref": "refs/heads/main",
                "vss_project_id": "vss_core_main",
                "active": True,
            },
        )
        assert create_binding_resp.status_code == 201
        binding_data = create_binding_resp.json()["resource"]
        binding_id = binding_data["binding_id"]

        list_bindings_resp = client.get(
            "/v1/admin/branch-bindings?frontend_project_id=h5vision/core",
            headers=admin_headers,
        )
        assert list_bindings_resp.status_code == 200
        assert len(list_bindings_resp.json()["items"]) == 1

        patch_binding_resp = client.patch(
            f"/v1/admin/branch-bindings/{binding_id}",
            headers=admin_headers,
            json={"frontend_workspace_name": "core_updated"},
        )
        assert patch_binding_resp.status_code == 200
        assert patch_binding_resp.json()["resource"]["frontend_workspace_name"] == "core_updated"

        # 12. Snapshots List & Detail
        snapshots_resp = client.get(
            f"/v1/admin/snapshots?repository_id={repo_id}",
            headers=admin_headers,
        )
        assert snapshots_resp.status_code == 200
        snapshots_list = snapshots_resp.json()["items"]
        assert len(snapshots_list) == 1
        snapshot_id = snapshots_list[0]["snapshot_id"]

        snapshot_detail_resp = client.get(
            f"/v1/admin/snapshots/{snapshot_id}",
            headers=admin_headers,
        )
        assert snapshot_detail_resp.status_code == 200
        snap_detail = snapshot_detail_resp.json()
        assert snap_detail["target_revision"] == commit_sha
        assert len(snap_detail["attempts"]) == 1

        # 13. VSS Projects Proxy
        vss_projects_resp = client.get(
            "/v1/admin/vss/projects",
            headers=admin_headers,
        )
        assert vss_projects_resp.status_code == 200
        vss_proj_items = vss_projects_resp.json()["items"]
        assert len(vss_proj_items) == 1
        assert vss_proj_items[0]["project_id"] == "vss_proj_1"

        # 14. Audit Logs Verification
        audit_logs_resp = client.get(
            "/v1/admin/audit-logs",
            headers=admin_headers,
        )
        assert audit_logs_resp.status_code == 200
        audit_items = audit_logs_resp.json()["items"]
        assert len(audit_items) >= 4
        action_names = [a["action"] for a in audit_items]
        assert "create_repository" in action_names
        assert "update_repository" in action_names
        assert "create_tracked_branch" in action_names
        assert "manual_sync" in action_names
        assert "create_branch_binding" in action_names

        # 15. Soft Deactivations
        delete_branch_resp = client.delete(
            f"/v1/admin/tracked-branches/{tracked_branch_id}",
            headers=admin_headers,
        )
        assert delete_branch_resp.status_code == 200
        assert delete_branch_resp.json()["resource"]["tracked"] is False

        delete_repo_resp = client.delete(
            f"/v1/admin/repositories/{repo_id}",
            headers=admin_headers,
        )
        assert delete_repo_resp.status_code == 200
        assert delete_repo_resp.json()["resource"]["active"] is False
