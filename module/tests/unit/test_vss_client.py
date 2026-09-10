from __future__ import annotations

import json

import httpx2
import pytest

from backend.core.config import Settings
from backend.integrations.vss.client import VssHttpClient
from backend.integrations.vss.errors import (
    VssAuthFailed,
    VssHttpContractMismatch,
    VssHttpRequestRejected,
    VssHttpUnavailable,
)
from backend.integrations.vss.schemas import VssIndexRequest, VssIndexState


def index_request() -> VssIndexRequest:
    return VssIndexRequest(
        project_root="/srv/snapshots/project/revision",
        project_id="project--main",
        note="snapshot revision",
    )


def client(handler, *, token: str | None = None) -> VssHttpClient:
    return VssHttpClient(
        base_url="http://vss.example:8200",
        token=token,
        transport=httpx2.MockTransport(handler),
    )


def test_start_index_sends_exact_http_contract_and_token() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "POST"
        assert request.url.path == "/index"
        assert request.headers["X-VSS-Token"] == "secret-token"
        body = json.loads(request.content)
        assert body == {
            "project_root": "/srv/snapshots/project/revision",
            "project_id": "project--main",
            "force": False,
            "briefing": True,
            "note": "snapshot revision",
        }
        assert "snapshot_id" not in body
        assert "expected_revision" not in body
        return httpx2.Response(
            202,
            json={"accepted": True, "project_id": "project--main", "state": "running"},
        )

    with client(handler, token="secret-token") as vss:
        response = vss.start_index(index_request())

    assert response.status_code == 202
    assert response.result.accepted is True
    assert response.result.state is VssIndexState.RUNNING


def test_start_index_preserves_already_running_409() -> None:
    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            409,
            json={
                "accepted": False,
                "reason": "already_running",
                "project_id": "project--main",
                "heartbeat_age_s": 1.4,
            },
        )

    with client(handler) as vss:
        response = vss.start_index(index_request())

    assert response.status_code == 409
    assert response.result.reason == "already_running"


def test_start_index_accepts_always_briefing_policy() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/index"
        body = json.loads(request.content)
        assert body["briefing"] == "always"
        return httpx2.Response(
            202,
            json={"accepted": True, "project_id": "project--main", "state": "running"},
        )

    request = index_request().model_copy(update={"briefing": "always"})
    with client(handler) as vss:
        response = vss.start_index(request)

    assert response.result.accepted is True


def test_query_routes_use_exact_paths_and_project_id() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.url.path, str(request.url.params.get("project_id", ""))))
        if request.url.path == "/index/status":
            return httpx2.Response(
                200,
                json={
                    "project_id": "project--main",
                    "state": "done",
                    "mode": "incremental",
                    "index": {
                        "commit": "2" * 40,
                        "mode": "incremental",
                        "incremental": {
                            "changed_files": 2,
                            "deleted_files": 1,
                            "unchanged_files": 7,
                            "reused_chunks": 30,
                            "rebuilt_chunks": 5,
                        },
                    },
                },
            )
        if request.url.path == "/index/exists":
            return httpx2.Response(
                200,
                json={"project_id": "project--main", "exists": True, "commit": "2" * 40},
            )
        if request.url.path == "/projects":
            assert request.url.params["project_id"] == "project--main"
            assert request.url.params["only"] == "current"
            assert request.url.params["files"] == "1"
            assert request.url.params["symbols"] == "1"
            return httpx2.Response(
                200,
                json={
                    "project_id": "project--main",
                    "index_id": "project--main",
                    "resolved_by": "exact",
                    "candidates": ["project--main"],
                    "projects": [
                        {
                            "project_id": "project--main",
                            "head_commit": "3" * 40,
                            "stale": True,
                            "current": True,
                            "dirty": False,
                        }
                    ],
                    "files": [
                        {
                            "path": "src/main.py",
                            "type": "code",
                            "chunks": 4,
                            "line_max": 87,
                            "symbols": ["main"],
                        }
                    ],
                    "incomplete": [],
                },
            )
        if request.url.path == "/health":
            return httpx2.Response(
                200,
                json={
                    "ok": True,
                    "store": "chroma",
                    "ollama": "http://127.0.0.1:11434",
                    "chat_model": "qwen2.5-coder:7b",
                    "embed_model": "bge-m3:latest",
                    "projects": ["project--main"],
                    "incomplete": [],
                },
            )
        if request.url.path == "/v1/models":
            return httpx2.Response(
                200,
                json={"models": ["qwen2.5-coder:7b"], "default": "qwen2.5-coder:7b"},
            )
        if request.url.path == "/briefing":
            return httpx2.Response(
                200,
                json={
                    "ok": True,
                    "project_id": "project--main",
                    "index_id": "project--main",
                    "briefing": "# Project briefing",
                    "commit": "2" * 40,
                    "quality_status": "partial",
                    "structure": {"entry_points": [{"path": "src/main.py"}]},
                },
            )
        if request.url.path == "/briefing/status":
            return httpx2.Response(
                200,
                json={
                    "project_id": "project--main",
                    "state": "ready",
                    "stage": "published",
                    "quality_status": "partial",
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    with client(handler) as vss:
        status = vss.status("project--main")
        exists = vss.exists("project--main")
        projects = vss.list_projects(
            project_id="project--main",
            only_current=True,
            include_files=True,
            include_symbols=True,
        )
        health = vss.health()
        models = vss.models()
        briefing = vss.briefing("project--main")
        briefing_status = vss.briefing_status("project--main")

    assert status.completed_for("2" * 40)
    assert status.mode == "incremental"
    assert status.index is not None
    assert status.index.mode == "incremental"
    assert status.index.incremental is not None
    assert status.index.incremental.reused_chunks == 30
    assert exists.exists is True
    assert projects.projects[0].project_id == "project--main"
    assert projects.projects[0].stale is True
    assert projects.index_id == "project--main"
    assert projects.resolved_by == "exact"
    assert projects.files[0].symbols == ["main"]
    assert health.store == "chroma"
    assert models.default == "qwen2.5-coder:7b"
    assert briefing.commit == "2" * 40
    assert briefing.quality_status == "partial"
    assert briefing.structure["entry_points"][0]["path"] == "src/main.py"
    assert briefing_status.state == "ready"
    assert briefing_status.quality_status == "partial"
    assert seen == [
        ("/index/status", "project--main"),
        ("/index/exists", "project--main"),
        ("/projects", "project--main"),
        ("/health", ""),
        ("/v1/models", ""),
        ("/briefing", "project--main"),
        ("/briefing/status", "project--main"),
    ]


def test_models_accept_no_loaded_default() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/models"
        return httpx2.Response(200, json={"models": [], "default": None})

    with client(handler) as vss:
        models = vss.models()

    assert models.default is None
    assert models.effective_default is None


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (401, VssAuthFailed),
        (400, VssHttpRequestRejected),
        (500, VssHttpUnavailable),
    ],
)
def test_http_errors_are_safely_classified(status_code: int, error_type: type[Exception]) -> None:
    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status_code, json={"error": "upstream detail with secret-token"})

    with client(handler, token="secret-token") as vss, pytest.raises(error_type) as captured:
        vss.health()

    assert "secret-token" not in str(captured.value)


def test_invalid_json_is_contract_mismatch() -> None:
    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"not-json")

    with client(handler) as vss, pytest.raises(VssHttpContractMismatch):
        vss.health()


def test_transport_failure_is_retryable_unavailable() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("offline", request=request)

    with client(handler) as vss, pytest.raises(VssHttpUnavailable) as captured:
        vss.health()

    assert captured.value.retryable is True


def test_factory_uses_settings_without_exposing_token() -> None:
    token = "settings-secret-token"

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["X-VSS-Token"] == token
        return httpx2.Response(
            200,
            json={
                "ok": True,
                "store": "chroma",
                "ollama": "http://127.0.0.1:11434",
                "chat_model": "qwen2.5-coder:7b",
                "embed_model": "bge-m3:latest",
            },
        )

    settings = Settings(vss_base_url="http://vss.example:8200", vss_token=token)
    with VssHttpClient.from_settings(
        settings, transport=httpx2.MockTransport(handler)
    ) as vss:
        assert vss.health().ok is True

    assert token not in repr(settings)


def test_start_status_and_accepted_flag_must_agree() -> None:
    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            202,
            json={"accepted": False, "reason": "already_running"},
        )

    with client(handler) as vss, pytest.raises(VssHttpContractMismatch):
        vss.start_index(index_request())


def test_delete_project_uses_exact_maintenance_contract() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/projects"
        assert request.url.params.get("project_id") == "project--main"
        return httpx2.Response(204)

    with client(handler) as vss:
        assert vss.delete_project(" project--main ") is None


def test_delete_project_preserves_unsupported_404() -> None:
    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, json={"detail": "not found"})

    with client(handler) as vss, pytest.raises(VssHttpRequestRejected) as captured:
        vss.delete_project("project--main")

    assert captured.value.upstream_status_code == 404
