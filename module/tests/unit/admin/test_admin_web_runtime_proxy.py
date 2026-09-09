from __future__ import annotations

import json
from pathlib import Path

import httpx2
from fastapi.testclient import TestClient
from pwdlib import PasswordHash

from admin_web.app import create_app
from admin_web.config import AdminWebSettings


def _settings(tmp_path: Path, *, role: str = "operator") -> AdminWebSettings:
    users_file = tmp_path / f"runtime-users-{role}.json"
    users_file.write_text(
        json.dumps(
            [
                {
                    "username": "alice",
                    "password_hash": PasswordHash.recommended().hash("secret-passphrase"),
                    "role": role,
                    "active": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    return AdminWebSettings(
        users_file=users_file,
        session_secret="s" * 48,
        backend_url="http://127.0.0.1:8000",
        backend_service_token="service-token-with-enough-entropy",
        backend_signing_secret="signing-secret-" * 3,
        allowed_origins=("http://admin.test",),
        secure_cookies=False,
        backend_timeout_seconds=30,
        runtime_model_timeout_seconds=210,
    )


def _login(client: TestClient) -> str:
    response = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "secret-passphrase"},
    )
    assert response.status_code == 200
    return response.json()["csrf_token"]


def test_runtime_model_lifecycle_routes_are_allowlisted_and_use_long_timeout(
    tmp_path: Path,
) -> None:
    captured: list[httpx2.Request] = []

    def backend(request: httpx2.Request) -> httpx2.Response:
        captured.append(request)
        return httpx2.Response(200, json={"ok": True})

    app = create_app(
        _settings(tmp_path),
        backend_transport=httpx2.MockTransport(backend),
    )
    with TestClient(app, base_url="http://admin.test") as client:
        csrf = _login(client)
        headers = {"Origin": "http://admin.test", "X-CSRF-Token": csrf}
        requests = (
            ("POST", "/v1/admin/runtime/models/run", {"model": "qwen3.8:27b"}),
            ("POST", "/v1/admin/runtime/models/up", {"model": "qwen3.8:27b"}),
            ("POST", "/v1/admin/runtime/models/down", {"model": "qwen3.8:27b"}),
            ("POST", "/v1/admin/runtime/models/reload", {"model": "qwen3.8:27b"}),
            (
                "PUT",
                "/v1/admin/runtime/models/auto-up",
                {"model": "qwen3.8:27b", "enabled": True},
            ),
        )
        for method, path, payload in requests:
            response = client.request(method, path, json=payload, headers=headers)
            assert response.status_code == 200

    assert [request.url.path for request in captured] == [
        "/v1/admin/runtime/models/run",
        "/v1/admin/runtime/models/up",
        "/v1/admin/runtime/models/down",
        "/v1/admin/runtime/models/reload",
        "/v1/admin/runtime/models/auto-up",
    ]
    for request in captured:
        timeout = request.extensions["timeout"]
        assert timeout["connect"] == 210
        assert timeout["read"] == 210
        assert timeout["write"] == 210
        assert timeout["pool"] == 210


def test_viewer_cannot_reach_runtime_model_lifecycle_mutations(tmp_path: Path) -> None:
    calls = 0

    def backend(_request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(200, json={"ok": True})

    app = create_app(
        _settings(tmp_path, role="viewer"),
        backend_transport=httpx2.MockTransport(backend),
    )
    with TestClient(app, base_url="http://admin.test") as client:
        csrf = _login(client)
        response = client.post(
            "/v1/admin/runtime/models/up",
            json={"model": "qwen3.8:27b"},
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
        )

    assert response.status_code == 403
    assert response.json()["reason"] == "ROLE_FORBIDDEN"
    assert calls == 0
