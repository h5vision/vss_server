from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pwdlib import PasswordHash

from admin_web.app import create_app
from admin_web.config import AdminWebSettings


def _settings(tmp_path: Path, *, secure_cookies: bool = False) -> AdminWebSettings:
    password_hash = PasswordHash.recommended().hash("correct horse battery staple")
    users_file = tmp_path / "users.json"
    users_file.write_text(
        json.dumps(
            [
                {
                    "username": "alice",
                    "password_hash": password_hash,
                    "role": "admin",
                    "active": True,
                },
                {
                    "username": "disabled",
                    "password_hash": password_hash,
                    "role": "viewer",
                    "active": False,
                },
            ]
        ),
        encoding="utf-8",
    )
    return AdminWebSettings(
        users_file=users_file,
        session_secret="s" * 48,
        backend_url="http://127.0.0.1:8000",
        backend_service_token="service-token-with-enough-entropy",
        backend_signing_secret="h" * 48,
        allowed_origins=("http://admin.test",),
        secure_cookies=secure_cookies,
        login_max_attempts=2,
    )


def test_login_session_cookie_and_logout_are_protected(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, secure_cookies=True))

    with TestClient(app, base_url="https://admin.test") as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        session = client.get("/api/auth/session")

        assert login.status_code == 200
        assert login.json() == {
            "authenticated": True,
            "username": "alice",
            "role": "admin",
            "csrf_token": login.json()["csrf_token"],
        }
        assert len(login.json()["csrf_token"]) >= 32
        cookie = login.headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "samesite=strict" in cookie
        assert "secure" in cookie
        assert "max-age=1800" in cookie
        assert session.json() == login.json()

        missing_proof = client.post("/api/auth/logout")
        assert missing_proof.status_code == 403
        assert missing_proof.json()["reason"] == "REQUEST_ORIGIN_REJECTED"

        logout = client.post(
            "/api/auth/logout",
            headers={
                "Origin": "http://admin.test",
                "X-CSRF-Token": login.json()["csrf_token"],
            },
        )
        assert logout.status_code == 204
        assert client.get("/api/auth/session").json() == {"authenticated": False}


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("missing", "correct horse battery staple"),
        ("disabled", "correct horse battery staple"),
        ("alice", "wrong password"),
    ],
)
def test_login_failures_are_generalized(
    tmp_path: Path, username: str, password: str
) -> None:
    app = create_app(_settings(tmp_path))

    with TestClient(app, base_url="http://admin.test") as client:
        response = client.post(
            "/api/auth/login", json={"username": username, "password": password}
        )

    assert response.status_code == 401
    assert response.json() == {
        "ok": False,
        "reason": "INVALID_CREDENTIALS",
        "detail": "Invalid username or password.",
        "retryable": False,
    }


def test_login_is_rate_limited_without_identifying_the_account(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))

    with TestClient(app, base_url="http://admin.test") as client:
        for _ in range(2):
            assert client.post(
                "/api/auth/login", json={"username": "alice", "password": "wrong"}
            ).status_code == 401
        limited = client.post(
            "/api/auth/login", json={"username": "alice", "password": "wrong"}
        )

    assert limited.status_code == 429
    assert limited.headers["retry-after"]
    assert limited.json()["reason"] == "LOGIN_RATE_LIMITED"
    assert "alice" not in limited.text


def test_settings_reject_non_loopback_backend_and_weak_secrets(tmp_path: Path) -> None:
    users_file = tmp_path / "users.json"
    users_file.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="loopback"):
        AdminWebSettings(
            users_file=users_file,
            session_secret="s" * 48,
            backend_url="https://backend.example.com",
            backend_service_token="service-token-with-enough-entropy",
            backend_signing_secret="h" * 48,
        )

    with pytest.raises(ValueError, match="32 bytes"):
        AdminWebSettings(
            users_file=users_file,
            session_secret="short",
            backend_url="http://127.0.0.1:8000",
            backend_service_token="service-token-with-enough-entropy",
            backend_signing_secret="short",
        )

    with pytest.raises(ValueError, match="must all be different"):
        AdminWebSettings(
            users_file=users_file,
            session_secret="s" * 48,
            backend_url="http://127.0.0.1:8000",
            backend_service_token="same-secret-" * 3,
            backend_signing_secret="same-secret-" * 3,
        )


def test_session_rechecks_active_user_and_role_from_the_registry(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)

    with TestClient(app, base_url="http://admin.test") as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        assert login.status_code == 200

        users = json.loads(settings.users_file.read_text(encoding="utf-8"))
        users[0]["role"] = "viewer"
        settings.users_file.write_text(json.dumps(users), encoding="utf-8")
        assert client.get("/api/auth/session").json()["role"] == "viewer"

        users[0]["active"] = False
        settings.users_file.write_text(json.dumps(users), encoding="utf-8")
        assert client.get("/api/auth/session").json() == {"authenticated": False}
        assert client.get("/v1/admin/repositories").status_code == 401


def test_login_fails_closed_when_user_registry_becomes_invalid(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)

    with TestClient(app, base_url="http://admin.test") as client:
        settings.users_file.write_text("not-json", encoding="utf-8")
        response = client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )

    assert response.status_code == 503
    assert response.json()["reason"] == "USER_REGISTRY_UNAVAILABLE"
    assert response.json()["retryable"] is True
