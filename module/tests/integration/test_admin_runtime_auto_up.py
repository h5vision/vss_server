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

from backend.app import create_app
from backend.core.config import Settings
from backend.features.admin.auth import canonical_admin_request
from backend.infrastructure.database.base import Base

SERVICE_TOKEN = "service-token-with-enough-entropy"
IDENTITY_SECRET = "identity-secret-with-at-least-32-bytes"


def _signed_request(client: TestClient, method: str, path: str, payload: dict):
    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    request_id = str(uuid4())
    content_sha256 = hashlib.sha256(body).hexdigest()
    canonical = canonical_admin_request(
        method=method,
        path_with_query=path,
        content_sha256=content_sha256,
        actor="kaypa",
        role="operator",
        timestamp=timestamp,
        request_id=request_id,
    )
    signature = hmac.new(IDENTITY_SECRET.encode(), canonical, hashlib.sha256).hexdigest()
    return client.request(
        method,
        path,
        content=body,
        headers={
            "Authorization": f"Bearer {SERVICE_TOKEN}",
            "X-Admin-Actor": "kaypa",
            "X-Admin-Role": "operator",
            "X-Admin-Timestamp": timestamp,
            "X-Admin-Request-ID": request_id,
            "X-Admin-Content-SHA256": content_sha256,
            "X-Admin-Signature": signature,
            "Content-Type": "application/json",
        },
    )


def _database(path: Path) -> str:
    engine = create_engine(
        f"sqlite:///{path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    Base.metadata.create_all(engine)
    engine.dispose()
    return f"sqlite+aiosqlite:///{path}"


def test_backend_auto_up_monitor_restores_model_without_browser_polling(tmp_path: Path) -> None:
    running = True
    restore_count = 0

    def ollama(request: httpx2.Request) -> httpx2.Response:
        nonlocal running, restore_count
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
            if payload["keep_alive"] == -1:
                restore_count += 1
                running = True
                return httpx2.Response(200, json={"done": True})
        return httpx2.Response(404)

    settings = Settings(
        vision_environment="test",
        docs_enabled=False,
        database_url=SecretStr(_database(tmp_path / "auto-up.db")),
        snapshot_materialization_root=tmp_path / "materialized",
        snapshot_admin_service_token=SecretStr(SERVICE_TOKEN),
        snapshot_admin_identity_secret=SecretStr(IDENTITY_SECRET),
        ollama_base_url="http://ollama.test:11434",
        ollama_auto_up_interval_seconds=0.05,
        snapshot_recovery_on_startup=False,
    )
    app = create_app(settings, ollama_transport=httpx2.MockTransport(ollama))

    with TestClient(app) as client:
        enabled = _signed_request(
            client,
            "PUT",
            "/v1/admin/runtime/models/auto-up",
            {"model": "qwen3.8:27b", "enabled": True},
        )
        assert enabled.status_code == 200
        assert enabled.json()["loaded_now"] is False

        # Simulate Ollama eviction or an external unload. No further Admin request
        # is made; the backend lifecycle task alone must restore the model.
        running = False
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not running:
            time.sleep(0.02)

        assert running is True
        assert restore_count == 1
