from __future__ import annotations

import json
from pathlib import Path

import httpx2
from fastapi.testclient import TestClient
from pwdlib import PasswordHash

from admin_web.app import create_app
from admin_web.config import AdminWebSettings


def _settings(tmp_path: Path, *, role: str) -> AdminWebSettings:
    users_file = tmp_path / f"service-restart-users-{role}.json"
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
    )


def _login(client: TestClient) -> str:
    response = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "secret-passphrase"},
    )
    assert response.status_code == 200
    return response.json()["csrf_token"]


def test_admin_service_restart_routes_are_allowlisted_and_signed(tmp_path: Path) -> None:
    captured: list[httpx2.Request] = []

    def backend(request: httpx2.Request) -> httpx2.Response:
        captured.append(request)
        if request.method == "GET":
            return httpx2.Response(
                200,
                json={
                    "ok": True,
                    "trigger_ready": True,
                    "scopes": ["snapshot_backend", "admin_web", "module_stack"],
                    "services": ["vss-snapshot.service", "vss-admin-web.service"],
                },
            )
        return httpx2.Response(
            202,
            json={
                "ok": True,
                "reason": "MODULE_SERVICE_RESTART_SCHEDULED",
                "detail": "scheduled",
                "retryable": False,
                "request_id": "00000000-0000-4000-8000-000000000001",
                "scope": "module_stack",
                "services": ["vss-snapshot.service", "vss-admin-web.service"],
                "already_scheduled": False,
                "reconnect_expected": True,
            },
        )

    app = create_app(
        _settings(tmp_path, role="admin"),
        backend_transport=httpx2.MockTransport(backend),
    )
    with TestClient(app, base_url="http://admin.test") as client:
        csrf = _login(client)
        status = client.get("/v1/admin/runtime/services")
        assert status.status_code == 200
        restart = client.post(
            "/v1/admin/runtime/services/restart",
            json={"scope": "module_stack"},
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
        )
        assert restart.status_code == 202

    assert [(request.method, request.url.path) for request in captured] == [
        ("GET", "/v1/admin/runtime/services"),
        ("POST", "/v1/admin/runtime/services/restart"),
    ]
    restart_request = captured[1]
    assert json.loads(restart_request.content) == {"scope": "module_stack"}
    assert restart_request.headers["Authorization"].startswith("Bearer ")
    assert restart_request.headers["X-Admin-Role"] == "admin"
    assert restart_request.headers["X-Admin-Signature"]


def test_operator_cannot_proxy_module_service_restart(tmp_path: Path) -> None:
    calls = 0

    def backend(_request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(202, json={"ok": True})

    app = create_app(
        _settings(tmp_path, role="operator"),
        backend_transport=httpx2.MockTransport(backend),
    )
    with TestClient(app, base_url="http://admin.test") as client:
        csrf = _login(client)
        response = client.post(
            "/v1/admin/runtime/services/restart",
            json={"scope": "snapshot_backend"},
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
        )

    assert response.status_code == 403
    assert response.json()["reason"] == "ROLE_FORBIDDEN"
    assert calls == 0
