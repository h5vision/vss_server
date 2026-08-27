"""Application assembly and readiness behavior."""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.core.config import Settings


def test_app_starts_without_live_dependencies_for_liveness() -> None:
    app = create_app(Settings(vision_environment="test", docs_enabled=False))

    with TestClient(app) as client:
        response = client.get("/v1/health")

    assert response.status_code == 200


def test_readiness_passes_when_phase_one_required_config_is_present() -> None:
    app = create_app(
        Settings(
            vision_environment="test",
            database_url="postgresql://db.example/vision",
            docs_enabled=False,
        )
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


def test_main_compatibility_entrypoint_exports_app() -> None:
    from main import app

    assert app.title == "Vision Snapshot Backend"
