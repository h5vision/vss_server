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
        "audit",
    ):
        assert f'data-view="{tab}"' in index.text
    assert 'id="repository-filter-select"' in index.text
    assert 'id="compare-commits-button"' in index.text
    assert "/commits" in script.text
    assert "/compare" in script.text
    assert "/materialize" in script.text
    assert "materialize-commit" in script.text
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
    assert "next_cursor" in script.text
    assert "const listPageSize = 25" in script.text
    assert "previousCursors" in script.text
    assert "payload?.reason" in script.text
    assert 'response.headers.get("X-Request-ID")' in script.text
    assert 'columns: ["project_id", "state", "commit"' in script.text
    assert 'columns: ["project_id", "active"' not in script.text
    assert 'id="previous-page"' in index.text
    assert 'id="next-page"' in index.text
    assert 'id="error-request-id"' in index.text
    assert 'id="modal-submit"' in index.text
    assert "/app.js?v=phase-3a3-final" in index.text
    assert 'byId("modal-submit").disabled = !readOnly' in script.text
    assert 'label: options.length ? "Repository 선택" : "Repository 없음"' in script.text
    assert "@media" in styles.text
