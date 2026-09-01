from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from uuid import UUID

import httpx2
from fastapi.testclient import TestClient
from pwdlib import PasswordHash

from admin_web.app import create_app
from admin_web.config import AdminWebSettings

FIXED_TIME = 1_788_200_000
FIXED_REQUEST_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def _settings(
    tmp_path: Path, role: str = "admin", max_request_body_bytes: int = 1024 * 1024
) -> AdminWebSettings:
    users_file = tmp_path / f"users-{role}.json"
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
        max_request_body_bytes=max_request_body_bytes,
    )


def _login(client: TestClient) -> str:
    response = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "secret-passphrase"},
    )
    assert response.status_code == 200
    UUID(response.headers["X-Request-ID"])
    return response.json()["csrf_token"]


def test_proxy_signs_the_exact_backend_contract_and_strips_browser_headers(
    tmp_path: Path,
) -> None:
    captured: list[httpx2.Request] = []

    def backend(request: httpx2.Request) -> httpx2.Response:
        captured.append(request)
        return httpx2.Response(200, json={"items": []})

    app = create_app(
        _settings(tmp_path),
        backend_transport=httpx2.MockTransport(backend),
        clock=lambda: FIXED_TIME,
        request_id_factory=lambda: FIXED_REQUEST_ID,
    )
    raw_query = "branch_ref=refs%2Fheads%2Fmain&cursor=a%2Bb"
    with TestClient(app, base_url="http://admin.test") as client:
        _login(client)
        response = client.get(
            f"/v1/admin/snapshots?{raw_query}",
            headers={
                "Authorization": "Bearer browser-token",
                "X-Admin-Actor": "mallory",
                "X-Admin-Role": "admin",
                "X-Admin-Signature": "forged",
                "Connection": "X-Remove-Me",
                "X-Remove-Me": "secret",
            },
        )

    assert response.status_code == 200
    request = captured[0]
    assert request.url.raw_path.decode("ascii") == f"/v1/admin/snapshots?{raw_query}"
    assert (
        request.headers["Authorization"]
        == "Bearer service-token-with-enough-entropy"
    )
    assert request.headers["X-Admin-Actor"] == "alice"
    assert request.headers["X-Admin-Role"] == "admin"
    assert request.headers["X-Admin-Timestamp"] == str(FIXED_TIME)
    assert request.headers["X-Admin-Request-ID"] == str(FIXED_REQUEST_ID)
    empty_hash = hashlib.sha256(b"").hexdigest()
    assert request.headers["X-Admin-Content-SHA256"] == empty_hash
    canonical = (
        "GET\n"
        f"/v1/admin/snapshots?{raw_query}\n"
        f"{empty_hash}\n"
        "alice\n"
        "admin\n"
        f"{FIXED_TIME}\n"
        f"{FIXED_REQUEST_ID}"
    )
    expected = hmac.new(
        ("signing-secret-" * 3).encode(), canonical.encode(), hashlib.sha256
    ).hexdigest()
    assert request.headers["X-Admin-Signature"] == expected
    assert "cookie" not in request.headers
    assert request.headers.get("connection") != "X-Remove-Me"
    assert "x-remove-me" not in request.headers


def test_mutation_requires_origin_csrf_and_role(tmp_path: Path) -> None:
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
        no_origin = client.post("/v1/admin/repositories", json={})
        bad_csrf = client.post(
            "/v1/admin/repositories",
            json={},
            headers={"Origin": "http://admin.test", "X-CSRF-Token": "wrong"},
        )
        forbidden = client.post(
            "/v1/admin/repositories",
            json={},
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
        )

    assert no_origin.status_code == 403
    assert no_origin.json()["reason"] == "REQUEST_ORIGIN_REJECTED"
    assert bad_csrf.status_code == 403
    assert bad_csrf.json()["reason"] == "CSRF_REJECTED"
    assert forbidden.status_code == 403
    assert forbidden.json()["reason"] == "ROLE_FORBIDDEN"
    assert calls == 0


def test_allowlist_rejects_unknown_paths_and_methods_before_backend(tmp_path: Path) -> None:
    calls = 0

    def backend(_request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(200, json={})

    app = create_app(
        _settings(tmp_path), backend_transport=httpx2.MockTransport(backend)
    )
    with TestClient(app, base_url="http://admin.test") as client:
        _login(client)
        repository_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        tracked_branch_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        binding_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        assert client.get(
            f"/v1/admin/repository-sync-runs?repository_id={repository_id}"
        ).status_code == 200
        assert client.get(
            f"/v1/admin/tracked-branches/{tracked_branch_id}/head-history"
        ).status_code == 200
        assert client.get("/v1/admin/branch-bindings").status_code == 200
        assert client.patch(
            f"/v1/admin/branch-bindings/{binding_id}",
            json={},
            headers={
                "Origin": "http://admin.test",
                "X-CSRF-Token": client.get("/api/auth/session").json()["csrf_token"],
            },
        ).status_code == 200
        unknown = client.get("/v1/admin/secrets")
        old_sync = client.get("/v1/admin/sync-runs")
        old_repository_sync = client.get(
            f"/v1/admin/repositories/{repository_id}/sync-runs"
        )
        old_history = client.get(
            f"/v1/admin/tracked-branches/{tracked_branch_id}/history"
        )
        wrong_method = client.put("/v1/admin/repositories")

    assert unknown.status_code == 404
    assert unknown.json()["reason"] == "ADMIN_ROUTE_NOT_ALLOWED"
    assert old_sync.status_code == 404
    assert old_repository_sync.status_code == 404
    assert old_history.status_code == 404
    assert wrong_method.status_code == 405
    assert wrong_method.json()["reason"] == "ADMIN_METHOD_NOT_ALLOWED"
    assert calls == 4


def test_backend_failures_are_always_structured(tmp_path: Path) -> None:
    def unavailable(_request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("private backend detail")

    app = create_app(
        _settings(tmp_path), backend_transport=httpx2.MockTransport(unavailable)
    )
    with TestClient(app, base_url="http://admin.test") as client:
        _login(client)
        response = client.get("/v1/admin/repositories")

    assert response.status_code == 503
    assert response.json()["reason"] == "BACKEND_UNAVAILABLE"
    assert response.json()["retryable"] is True
    assert response.json()["request_id"]
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    assert "private backend detail" not in response.text


def test_proxy_rejects_declared_and_actual_oversized_bodies(tmp_path: Path) -> None:
    calls = 0

    def backend(_request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(200, json={})

    app = create_app(
        _settings(tmp_path, max_request_body_bytes=8),
        backend_transport=httpx2.MockTransport(backend),
        request_id_factory=lambda: FIXED_REQUEST_ID,
    )
    proof = {"Origin": "http://admin.test"}
    with TestClient(app, base_url="http://admin.test") as client:
        proof["X-CSRF-Token"] = _login(client)
        declared = client.post(
            "/v1/admin/repositories",
            content=b"",
            headers={**proof, "Content-Length": "9"},
        )
        actual = client.post(
            "/v1/admin/repositories",
            content=b"123456789",
            headers=proof,
        )

    for response in (declared, actual):
        assert response.status_code == 413
        assert response.json()["reason"] == "REQUEST_BODY_TOO_LARGE"
        assert response.headers["X-Request-ID"] == str(FIXED_REQUEST_ID)
    assert calls == 0


def test_no_cors_policy_is_installed(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))

    with TestClient(app, base_url="http://admin.test") as client:
        response = client.options(
            "/api/auth/session",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert "access-control-allow-origin" not in response.headers
