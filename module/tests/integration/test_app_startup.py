"""Application assembly and readiness behavior."""

from __future__ import annotations

import httpx2
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.core.config import Settings


def ready_vss(request: httpx2.Request) -> httpx2.Response:
    if request.url.path == "/health":
        return httpx2.Response(
            200,
            json={
                "ok": True,
                "store": "pgvector",
                "ollama": "http://127.0.0.1:11434",
                "chat_model": "qwen2.5-coder:7b",
                "embed_model": "bge-m3:latest",
                "projects": ["vision--frontend"],
            },
        )
    if request.url.path == "/projects":
        return httpx2.Response(200, json={"projects": [], "incomplete": []})
    raise AssertionError(f"unexpected VSS path: {request.url.path}")


def test_app_starts_without_live_dependencies_for_liveness() -> None:
    app = create_app(Settings(vision_environment="test", docs_enabled=False))

    with TestClient(app) as client:
        response = client.get("/v1/health")

    assert response.status_code == 200


def test_readiness_passes_when_phase_one_required_config_is_present() -> None:
    app = create_app(
        Settings(
            vision_environment="test",
            database_url="sqlite+aiosqlite:///:memory:",
            docs_enabled=False,
        ),
        vss_transport=httpx2.MockTransport(ready_vss),
    )

    with TestClient(app) as client:
        response = client.get("/v1/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "service": "Vision Snapshot Backend",
        "version": "0.1.0",
        "status": "ready",
    }


def test_readiness_reports_vss_contract_failure() -> None:
    def invalid_vss(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"ok": True})

    app = create_app(
        Settings(
            vision_environment="test",
            database_url="sqlite+aiosqlite:///:memory:",
            docs_enabled=False,
        ),
        vss_transport=httpx2.MockTransport(invalid_vss),
    )

    with TestClient(app) as client:
        response = client.get("/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["reason"] == "VSS_HTTP_CONTRACT_MISMATCH"
    assert response.json()["retryable"] is False


def test_main_compatibility_entrypoint_exports_app() -> None:
    from main import app

    assert app.title == "Vision Snapshot Backend"


def test_provider_and_tag_collectors_are_wired_only_when_enabled() -> None:
    app = create_app(
        Settings(
            vision_environment="test",
            database_url="sqlite+aiosqlite:///:memory:",
            snapshot_change_request_collection_enabled=True,
            snapshot_tag_collection_enabled=True,
            docs_enabled=False,
        ),
        github_transport=httpx2.MockTransport(
            lambda _request: httpx2.Response(200, json=[])
        ),
        gitlab_transport=httpx2.MockTransport(
            lambda _request: httpx2.Response(200, json=[])
        ),
    )

    with TestClient(app):
        assert app.state.change_request_service is not None
        assert app.state.repository_tag_service is not None
