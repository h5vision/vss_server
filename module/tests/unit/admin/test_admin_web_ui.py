from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from pwdlib import PasswordHash

from admin_web.app import create_app
from admin_web.config import AdminWebSettings


def _app(tmp_path: Path):
    users_file = tmp_path / "users.json"
    users_file.write_text(
        json.dumps(
            [
                {
                    "username": "viewer",
                    "password_hash": PasswordHash.recommended().hash("viewer-password"),
                    "role": "viewer",
                    "active": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    return create_app(
        AdminWebSettings(
            users_file=users_file,
            session_secret="s" * 48,
            backend_url="http://127.0.0.1:8000",
            backend_service_token="service-token-with-enough-entropy",
            backend_signing_secret="h" * 48,
            secure_cookies=False,
        )
    )


def test_real_static_ui_exposes_required_operational_views(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path), base_url="http://admin.test") as client:
        index = client.get("/")
        styles = client.get("/styles.css")
        script = client.get("/app.js")

    assert index.status_code == styles.status_code == script.status_code == 200
    assert index.headers["Cache-Control"] == "no-cache"
    assert styles.headers["Cache-Control"] == "no-cache"
    assert script.headers["Cache-Control"] == "no-cache"
    assert "Repository" in index.text
    for tab in (
        "repositories",
        "tracked-branches",
        "branch-bindings",
        "sync-history",
        "snapshots",
        "commits",
        "vss",
        "chat",
        "vss-requests",
        "audit",
    ):
        assert f'data-view="{tab}"' in index.text
    assert 'id="repository-filter-select"' in index.text
    assert 'id="compare-commits-button"' in index.text
    assert "/commits" in script.text
    assert "/compare" in script.text
    assert "/v1/admin/repositories/discover?remote_url=" in script.text
    assert "wireRepositoryRegistrationDiscovery" in script.text
    assert "default_branch_ref" in script.text
    assert "commitWebUrl" in script.text
    assert 'code.target = "_blank"' in script.text
    assert 'code.rel = "noopener noreferrer"' in script.text
    assert ".repository-discovery-status" in styles.text
    assert ".sha-link" in styles.text
    assert "/materialize" in script.text
    assert "materialize-commit" in script.text
    assert 'body: JSON.stringify({})' in script.text
    assert "body: {}" not in script.text
    assert 'role="dialog"' in index.text
    assert "loading" in script.text
    assert "empty" in script.text
    assert "error" in script.text
    assert "data-min-role" in index.text
    assert "/v1/admin/repository-sync-runs" in script.text
    assert "/head-history" in script.text
    assert "/v1/admin/branch-bindings" in script.text
    assert "/v1/admin/sync-runs" not in script.text
    assert "const form = event.currentTarget" in script.text
    assert "form.reset()" in script.text
    assert "/branches`" in script.text
    assert 'byId("modal-error").hidden = true' in script.text
    assert '"PATCH"' in script.text
    assert "/v1/admin/snapshots/${encodeURIComponent(snapshotId)}" in script.text
    assert "return row.snapshot_id || row.binding_id || row.tracked_branch_id" in script.text
    assert 'new Set(["failed", "rejected", "aborted"])' in script.text
    assert 'row.state === "materialized"' in script.text
    assert '"index-snapshot"' in script.text
    assert '/v1/admin/snapshots/${id}/index' in script.text
    assert '"index-tracked-branch"' in script.text
    assert '/v1/admin/tracked-branches/${id}/index' in script.text
    assert '"purge-repository"' in script.text
    assert '/purge?confirm=${encodeURIComponent(repositoryId)}' in script.text
    assert '"delete-vss-project"' in script.text
    assert '/vss/projects/${id}?confirm=${encodeURIComponent(projectId)}' in script.text
    assert "row.tracked && row.current_head_sha" in script.text
    assert 'byId("action-modal").close()' in script.text
    assert "closeModal()" not in script.text
    assert "next_cursor" in script.text
    assert "const listPageSize = 25" in script.text
    assert "previousCursors" in script.text
    assert "payload?.reason" in script.text
    assert 'response.status === 401 && payload?.reason === "AUTHENTICATION_REQUIRED"' in script.text
    assert "if (response.status === 401) {" not in script.text
    assert 'response.headers.get("X-Request-ID")' in script.text
    assert 'columns: ["project_id", "state", "commit"' in script.text
    assert 'columns: ["project_id", "active"' not in script.text
    assert 'id="previous-page"' in index.text
    assert 'id="next-page"' in index.text
    assert 'id="error-request-id"' in index.text
    assert 'id="modal-submit"' in index.text
    assert 'id="runtime-models"' in index.text
    assert 'id="runtime-model-up"' in index.text
    assert 'id="runtime-model-down"' in index.text
    assert 'id="runtime-model-reload"' in index.text
    assert 'id="runtime-model-auto-up"' in index.text
    assert 'data-min-role="operator"' in index.text
    assert "/v1/admin/vss/request-failures" in script.text
    assert "/v1/admin/runtime/models" in script.text
    assert '/v1/admin/runtime/models/${action}' in script.text
    assert "/v1/admin/runtime/models/auto-up" in script.text
    assert "installed_models" in script.text
    assert "stopped_models" in script.text
    assert "auto_up_models" in script.text
    assert 'group.label = "Running"' in script.text
    assert 'group.label = "Stopped"' in script.text
    assert "runtimeModelLoading" in script.text
    assert "runtimeModelsSignature" in script.text
    assert "syncRuntimeModelControls" in script.text
    assert "setInterval(refreshRuntimeModels" in script.text
    assert "/app.js?v=apple-ui-v2" in index.text
    assert "/styles.css?v=apple-ui-v2" in index.text
    assert 'class="sidebar-section"' in index.text
    assert 'class="sidebar-label">Repository</h2>' in index.text
    assert "--apple-blue: #007aff" in styles.text
    assert ".runtime-model-auto-up input:checked" in styles.text
    assert ".chat-message-row.user .chat-bubble" in styles.text
    assert "repository-metadata-details" in script.text
    assert "metadataDetails.open = false" in script.text
    assert "timer = setTimeout(() => void discover(), 180)" in script.text
    assert 'status.setAttribute("aria-live", "polite")' in script.text
    assert "prefers-reduced-motion" in styles.text
    assert "prefers-reduced-transparency" in styles.text
    assert "prefers-contrast: more" in styles.text
    assert "prefers-color-scheme: dark" in styles.text
    assert "backdrop-filter" in styles.text
    assert "button:active:not(:disabled)" in styles.text
    assert 'id="confirm-modal"' in index.text
    assert 'id="confirm-title"' in index.text
    assert 'id="confirm-message"' in index.text
    assert 'id="confirm-submit"' in index.text
    assert 'id="confirm-cancel"' in index.text
    assert "confirmAdminAction" in script.text
    assert "window.confirm(" not in script.text
    assert 'byId("modal-submit").disabled = !readOnly' in script.text
    assert 'label: options.length ? "Repository 선택" : "Repository 없음"' in script.text
    assert 'id="module-service-control"' in index.text
    assert 'id="restart-snapshot-backend"' in index.text
    assert 'id="restart-admin-web"' in index.text
    assert 'id="restart-module-stack"' in index.text
    assert 'data-restart-scope="snapshot_backend"' in index.text
    assert 'data-restart-scope="admin_web"' in index.text
    assert 'data-restart-scope="module_stack"' in index.text
    assert 'data-min-role="admin"' in index.text
    assert "/v1/admin/runtime/services" in script.text
    assert "/v1/admin/runtime/services/restart" in script.text
    assert "restartModuleServices" in script.text
    assert "waitForModuleServiceRecovery" in script.text
    assert "confirmAdminAction" in script.text
    assert "Restart channel not configured" in script.text
    assert ".module-service-control" in styles.text
    assert "@media" in styles.text
    assert 'id="chat-view"' in index.text
    assert 'id="chat-session-list"' in index.text
    assert 'id="chat-transcript"' in index.text
    assert 'id="chat-trace-inspector"' in index.text
    assert "/v1/admin/chat/conversations" in script.text
    assert "/v1/admin/chat/responses/${encodeURIComponent(responseId)}/trace" in script.text
    assert "loadChatConversation" in script.text
    assert "renderChatTranscript" in script.text
    assert "renderChatTrace" in script.text
    assert ".chat-observability" in styles.text
    assert ".chat-message-row.user" in styles.text
    assert ".chat-trace-inspector" in styles.text
    assert 'id="chat-session-filter"' in index.text
    assert "chatMonitorRefreshMs = 3_000" in script.text
    assert "setInterval(refreshChatMonitor" in script.text
    assert "last_chat_model" in script.text
    assert 'title.textContent = "Sources"' in script.text
    assert 'id="chat-retention-button"' in index.text
    assert "/v1/admin/chat/retention" in script.text
    assert "/v1/admin/chat/retention/purge?confirm=purge-expired" in script.text
    assert "deleteSelectedChatConversation" in script.text
    assert "purgeExpiredChatConversations" in script.text
    assert ".chat-conversation-actions" in styles.text
    assert ".chat-pane-actions" in styles.text
