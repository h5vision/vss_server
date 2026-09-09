from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx2
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app import create_app
from backend.core.config import Settings
from backend.features.admin.auth import canonical_admin_request
from backend.infrastructure.database.base import Base
from backend.infrastructure.database.models import AuditLog

SERVICE_TOKEN = "service-token-with-enough-entropy"
IDENTITY_SECRET = "identity-secret-with-at-least-32-bytes"


def _signed_request(
    client: TestClient,
    method: str,
    path: str,
    *,
    role: str,
    payload: dict | None = None,
):
    body = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    request_id = str(uuid4())
    content_sha256 = hashlib.sha256(body).hexdigest()
    canonical = canonical_admin_request(
        method=method,
        path_with_query=path,
        content_sha256=content_sha256,
        actor="kaypa",
        role=role,
        timestamp=timestamp,
        request_id=request_id,
    )
    signature = hmac.new(IDENTITY_SECRET.encode(), canonical, hashlib.sha256).hexdigest()
    headers = {
        "Authorization": f"Bearer {SERVICE_TOKEN}",
        "X-Admin-Actor": "kaypa",
        "X-Admin-Role": role,
        "X-Admin-Timestamp": timestamp,
        "X-Admin-Request-ID": request_id,
        "X-Admin-Content-SHA256": content_sha256,
        "X-Admin-Signature": signature,
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    return client.request(method, path, content=body, headers=headers)


def _database(path: Path):
    db_url = f"sqlite+aiosqlite:///{path}"
    engine = create_engine(
        f"sqlite:///{path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    Base.metadata.create_all(engine)
    return db_url, engine


def test_admin_runtime_model_lifecycle_up_auto_up_reload_down_and_audit(tmp_path: Path) -> None:
    db_url, sync_engine = _database(tmp_path / "runtime-lifecycle.db")
    running = False
    keep_alive_values: list[int] = []

    def ollama(request: httpx2.Request) -> httpx2.Response:
        nonlocal running
        if request.url.path == "/api/tags":
            return httpx2.Response(200, json={"models": [{"name": "qwen3.8:27b"}]})
        if request.url.path == "/api/ps":
            return httpx2.Response(
                200,
                json={"models": [{"name": "qwen3.8:27b"}] if running else []},
            )
        if request.url.path == "/api/show":
            return httpx2.Response(200, json={"capabilities": ["completion"]})
        if request.url.path == "/api/generate":
            payload = json.loads(request.content)
            keep_alive_values.append(payload["keep_alive"])
            running = payload["keep_alive"] != 0
            return httpx2.Response(200, json={"done": True})
        return httpx2.Response(404)

    settings = Settings(
        vision_environment="test",
        docs_enabled=False,
        database_url=SecretStr(db_url),
        snapshot_materialization_root=tmp_path / "materialized",
        snapshot_admin_service_token=SecretStr(SERVICE_TOKEN),
        snapshot_admin_identity_secret=SecretStr(IDENTITY_SECRET),
        ollama_base_url="http://ollama.test:11434",
        snapshot_recovery_on_startup=False,
    )
    app = create_app(settings, ollama_transport=httpx2.MockTransport(ollama))

    with TestClient(app) as client:
        viewer_up = _signed_request(
            client,
            "POST",
            "/v1/admin/runtime/models/up",
            role="viewer",
            payload={"model": "qwen3.8:27b"},
        )
        assert viewer_up.status_code == 403

        up = _signed_request(
            client,
            "POST",
            "/v1/admin/runtime/models/up",
            role="operator",
            payload={"model": "qwen3.8:27b"},
        )
        assert up.status_code == 200
        assert up.json()["already_running"] is False
        assert up.json()["models"] == ["qwen3.8:27b"]

        auto_up = _signed_request(
            client,
            "PUT",
            "/v1/admin/runtime/models/auto-up",
            role="operator",
            payload={"model": "qwen3.8:27b", "enabled": True},
        )
        assert auto_up.status_code == 200
        assert auto_up.json()["enabled"] is True
        assert auto_up.json()["loaded_now"] is False
        assert auto_up.json()["auto_up_models"] == ["qwen3.8:27b"]

        reload_response = _signed_request(
            client,
            "POST",
            "/v1/admin/runtime/models/reload",
            role="operator",
            payload={"model": "qwen3.8:27b"},
        )
        assert reload_response.status_code == 200
        assert reload_response.json()["was_running"] is True
        assert reload_response.json()["auto_up_models"] == ["qwen3.8:27b"]

        down = _signed_request(
            client,
            "POST",
            "/v1/admin/runtime/models/down",
            role="operator",
            payload={"model": "qwen3.8:27b"},
        )
        assert down.status_code == 200
        assert down.json()["already_stopped"] is False
        assert down.json()["auto_up_disabled"] is True
        assert down.json()["models"] == []
        assert down.json()["auto_up_models"] == []

        state = _signed_request(
            client,
            "GET",
            "/v1/admin/runtime/models",
            role="viewer",
        )
        assert state.status_code == 200
        assert state.json()["stopped_models"] == ["qwen3.8:27b"]
        assert state.json()["auto_up_models"] == []

    assert keep_alive_values == [-1, 0, -1, 0]
    with Session(sync_engine) as session:
        actions = {
            row.action
            for row in session.query(AuditLog)
            .filter(AuditLog.target_id == "qwen3.8:27b")
            .all()
        }
    assert {
        "up_ollama_model",
        "set_ollama_model_auto_up",
        "reload_ollama_model",
        "down_ollama_model",
    }.issubset(actions)
    sync_engine.dispose()
