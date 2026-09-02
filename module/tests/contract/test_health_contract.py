"""Frontend-facing health and common error contracts."""

from __future__ import annotations

from uuid import UUID

from fastapi.testclient import TestClient
from pydantic import BaseModel

from backend.app import create_app
from backend.core.config import Settings


class ValidationPayload(BaseModel):
    count: int


def _client(*, database_url: str | None = None) -> TestClient:
    settings = Settings(
        vision_environment="test",
        database_url=database_url,
        docs_enabled=False,
    )
    return TestClient(create_app(settings))


def test_frontend_health_contract_is_fast_and_local() -> None:
    with _client() as client:
        response = client.get("/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "service": "Vision Snapshot Backend",
        "version": "0.1.0",
        "status": "alive",
    }
    UUID(response.headers["X-Request-ID"])
    assert response.elapsed.total_seconds() < 2


def test_readiness_failure_has_machine_and_human_reasons() -> None:
    with _client() as client:
        response = client.get("/v1/health/ready")

    body = response.json()
    assert response.status_code == 503
    assert body["ok"] is False
    assert body["reason"] == "SERVICE_NOT_READY"
    assert body["detail"] == "Required runtime configuration is incomplete."
    assert body["retryable"] is False
    assert body["missing"] == ["DATABASE_URL"]
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_unknown_route_is_still_a_structured_json_error() -> None:
    with _client() as client:
        response = client.get("/v1/not-found")

    body = response.json()
    assert response.status_code == 404
    assert body["ok"] is False
    assert body["reason"] == "HTTP_ERROR"
    assert body["detail"] == "Not Found"
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_validation_error_does_not_echo_input_content() -> None:
    app = create_app(Settings(vision_environment="test", docs_enabled=False))

    @app.post("/test/validation")
    def validate(payload: ValidationPayload) -> dict[str, bool]:
        return {"ok": True}

    secret_input = "must-not-be-echoed"
    with TestClient(app) as client:
        response = client.post("/test/validation", json={"count": secret_input})

    body = response.json()
    assert response.status_code == 422
    assert body["reason"] == "REQUEST_VALIDATION_FAILED"
    assert body["retryable"] is False
    assert secret_input not in response.text
    assert set(body["errors"][0]) == {"location", "message", "type"}


def test_unhandled_error_does_not_echo_exception_content(caplog) -> None:
    app = create_app(Settings(vision_environment="test", docs_enabled=False))

    @app.get("/test/unhandled")
    def fail() -> None:
        raise RuntimeError("must-not-be-logged-or-returned")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/test/unhandled")

    body = response.json()
    assert response.status_code == 500
    assert body["reason"] == "INTERNAL_SERVER_ERROR"
    assert "must-not-be-logged-or-returned" not in response.text
    assert "must-not-be-logged-or-returned" not in caplog.text
