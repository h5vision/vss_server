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
    assert "Repository" in index.text
    for tab in (
        "repositories",
        "tracked-branches",
        "branch-bindings",
        "sync-history",
        "snapshots",
        "vss",
        "audit",
    ):
        assert f'data-view="{tab}"' in index.text
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
    assert "@media" in styles.text
