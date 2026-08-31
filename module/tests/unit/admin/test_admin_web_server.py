"""Unit tests for the independent Admin Web server and BFF proxy (Port 4180)."""

from __future__ import annotations

import httpx2
from starlette.testclient import TestClient

from admin_web.server import create_admin_web_app
from backend.app import create_app
from backend.core.config import Settings


def test_admin_web_health():
    app = create_admin_web_app()
    client = TestClient(app)

    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "admin_web"
    assert data["port"] == 4180


def test_admin_web_serves_static_assets():
    app = create_admin_web_app()
    client = TestClient(app)

    # 1. Root index.html
    root_resp = client.get("/")
    assert root_resp.status_code == 200
    assert "<title>Vision Snapshot Admin" in root_resp.text
    assert "Admin Portal :4180" in root_resp.text

    # 2. styles.css
    css_resp = client.get("/styles.css")
    assert css_resp.status_code == 200
    assert "--font-sans" in css_resp.text

    # 3. app.js
    js_resp = client.get("/app.js")
    assert js_resp.status_code == 200
    assert "class AdminApp" in js_resp.text


def test_admin_web_proxies_to_backend_successfully():
    def mock_backend_handler(request: httpx2.Request) -> httpx2.Response:
        assert request.headers.get("x-admin-token") == "secret-admin-token"
        if request.url.path == "/v1/admin/repositories":
            item = {
                "repository_id": "00000000-0000-0000-0000-000000000001",
                "name": "test-repo",
                "remote_url": "https://github.com/test/repo.git",
                "default_branch": "refs/heads/main",
                "active": True,
                "created_at": "2026-08-31T00:00:00Z",
                "updated_at": "2026-08-31T00:00:00Z",
            }
            return httpx2.Response(200, json={"items": [item]})
        return httpx2.Response(404, json={"reason": "NOT_FOUND"})

    mock_transport = httpx2.MockTransport(mock_backend_handler)
    app = create_admin_web_app(
        backend_base_url="http://mock-backend:8000",
        backend_transport=mock_transport,
    )
    client = TestClient(app)

    response = client.get(
        "/v1/admin/repositories",
        headers={"X-Admin-Token": "secret-admin-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["name"] == "test-repo"


def test_admin_web_handles_backend_unavailability():
    def failing_backend_handler(_request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("Connection refused")

    mock_transport = httpx2.MockTransport(failing_backend_handler)
    app = create_admin_web_app(
        backend_base_url="http://unreachable-backend:8000",
        backend_transport=mock_transport,
    )
    client = TestClient(app)

    response = client.get("/v1/admin/repositories")
    assert response.status_code == 503
    data = response.json()
    assert data["reason"] == "BACKEND_UNAVAILABLE"
    assert data["retryable"] is True


def test_backend_app_mounts_admin_web_at_admin_path(tmp_path):
    settings = Settings(
        database_url=None,
        vss_base_url="http://127.0.0.1:8200",
        vss_token="secret",
        snapshot_materialization_root=tmp_path / "mat",
        snapshot_collection_root=tmp_path / "col",
    )
    backend_app = create_app(settings=settings)
    client = TestClient(backend_app)

    resp = client.get("/admin/")
    assert resp.status_code == 200
    assert "<title>Vision Snapshot Admin" in resp.text

