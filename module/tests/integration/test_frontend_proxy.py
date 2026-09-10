"""FastAPI to DB binding to fake VSS read-only proxy integration."""

from __future__ import annotations

from uuid import uuid4

import httpx2
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app import create_app
from backend.core.config import Settings
from backend.infrastructure.database.base import Base
from backend.infrastructure.database.models import BranchBinding, Repository


def seed_binding(database_path: str) -> None:
    engine = create_engine(
        f"sqlite:///{database_path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        repository = Repository(
            repository_id=uuid4(),
            canonical_name="h5vision/vision",
            display_name="Vision",
            provider="github",
            remote_url="https://github.com/h5vision/vision.git",
            default_branch_ref="refs/heads/frontend",
        )
        session.add(repository)
        session.flush()
        session.add(
            BranchBinding(
                frontend_project_id="h5vision/vision",
                frontend_workspace_name="vision",
                repository_id=repository.repository_id,
                branch_ref="refs/heads/frontend",
                vss_project_id="vision--frontend",
                active=True,
            )
        )
        session.commit()
    engine.dispose()


def test_frontend_read_routes_transform_and_redact_vss_responses(tmp_path) -> None:
    database_path = str(tmp_path / "snapshot.db")
    seed_binding(database_path)
    seen: list[tuple[str, str]] = []

    def fake_vss(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.url.path, str(request.url.params.get("project_id", ""))))
        if request.url.path == "/projects":
            return httpx2.Response(
                200,
                json={
                    "projects": [
                        {
                            "project_id": "vision--frontend",
                            "state": "done",
                            "chunks": 83,
                            "commit": "2" * 40,
                            "project_root": "/srv/private/vision",
                        }
                    ],
                    "incomplete": [],
                },
            )
        if request.url.path == "/v1/models":
            return httpx2.Response(
                200,
                json={
                    "models": ["qwen2.5-coder:7b", "qwen3:8b"],
                    "default": "qwen2.5-coder:7b",
                },
            )
        if request.url.path == "/briefing":
            assert request.url.params["project_id"] == "vision--frontend"
            return httpx2.Response(
                200,
                json={
                    "ok": True,
                    "project_id": "vision--frontend",
                    "index_id": "vision--frontend",
                    "briefing": "# Vision",
                    "commit": "2" * 40,
                    "md_path": "/srv/private/briefings/vision.md",
                },
            )
        raise AssertionError(f"unexpected VSS path: {request.url.path}")

    app = create_app(
        Settings(
            vision_environment="test",
            database_url=f"sqlite+aiosqlite:///{database_path}",
            vss_base_url="http://vss.example:8200",
            docs_enabled=False,
        ),
        vss_transport=httpx2.MockTransport(fake_vss),
    )

    with TestClient(app) as client:
        projects = client.get("/v1/projects")
        models = client.get("/v1/models")
        briefing = client.get("/v1/briefing", params={"project_id": "vision"})

    assert projects.status_code == 200
    assert projects.json()["projects"][0] == {
        "project_id": "vision--frontend",
        "name": "vision--frontend",
        "commit": "2" * 40,
        "state": "done",
        "chunks": 83,
        "indexed_at": None,
        "note": None,
    }
    assert "/srv/private" not in projects.text

    assert models.status_code == 200
    assert models.json()["default_model_id"] == "qwen2.5-coder:7b"
    assert models.json()["models"][0]["display_name"] == "qwen2.5-coder:7b"

    assert briefing.status_code == 200
    assert briefing.json()["project_id"] == "vision"
    assert briefing.json()["index_id"] == "vision--frontend"
    assert "/srv/private" not in briefing.text
    assert seen == [
        ("/projects", ""),
        ("/v1/models", ""),
        ("/briefing", "vision--frontend"),
    ]


def test_briefing_requires_an_exact_active_binding(tmp_path) -> None:
    database_path = str(tmp_path / "snapshot.db")
    seed_binding(database_path)

    def no_vss_call(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError(f"VSS must not be called: {request.url}")

    app = create_app(
        Settings(
            vision_environment="test",
            database_url=f"sqlite+aiosqlite:///{database_path}",
            docs_enabled=False,
        ),
        vss_transport=httpx2.MockTransport(no_vss_call),
    )

    with TestClient(app) as client:
        response = client.get("/v1/briefing", params={"project_id": "unbound-workspace"})

    assert response.status_code == 409
    assert response.json()["reason"] == "SNAPSHOT_DESTINATION_REQUIRED"
    assert response.json()["retryable"] is False


def test_frontend_proxy_returns_safe_vss_unavailable_error() -> None:
    def unavailable(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("private upstream detail", request=request)

    app = create_app(
        Settings(vision_environment="test", docs_enabled=False),
        vss_transport=httpx2.MockTransport(unavailable),
    )

    with TestClient(app) as client:
        response = client.get("/v1/projects")

    body = response.json()
    assert response.status_code == 503
    assert body["reason"] == "VSS_HTTP_UNAVAILABLE"
    assert body["retryable"] is True
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert "private upstream detail" not in response.text
