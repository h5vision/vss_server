from __future__ import annotations

import json
from pathlib import Path

import httpx2
from fastapi.testclient import TestClient
from pwdlib import PasswordHash

from admin_web.app import create_app
from admin_web.config import AdminWebSettings


def _settings(tmp_path: Path) -> AdminWebSettings:
    users_file = tmp_path / "users.json"
    users_file.write_text(
        json.dumps(
            [
                {
                    "username": "alice",
                    "password_hash": PasswordHash.recommended().hash("secret-passphrase"),
                    "role": "admin",
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
    )


def test_destructive_repository_and_vector_routes_are_allowlisted_for_admin(
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
    repository_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    with TestClient(app, base_url="http://admin.test") as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "secret-passphrase"},
        )
        csrf = login.json()["csrf_token"]
        headers = {"Origin": "http://admin.test", "X-CSRF-Token": csrf}
        repository = client.delete(
            f"/v1/admin/repositories/{repository_id}/purge?confirm={repository_id}",
            headers=headers,
        )
        vector = client.delete(
            "/v1/admin/vss/projects/vss_server%40test-merge%40test-merge"
            "?confirm=vss_server%40test-merge%40test-merge",
            headers=headers,
        )

    assert repository.status_code == 200
    assert vector.status_code == 200
    assert [request.method for request in captured] == ["DELETE", "DELETE"]
    assert captured[0].url.path == f"/v1/admin/repositories/{repository_id}/purge"
    assert captured[1].url.path == "/v1/admin/vss/projects/vss_server@test-merge@test-merge"
    assert captured[1].url.raw_path.decode("ascii") == (
        "/v1/admin/vss/projects/vss_server%40test-merge%40test-merge"
        "?confirm=vss_server%40test-merge%40test-merge"
    )
