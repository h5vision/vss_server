from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from uuid import uuid4

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


def _settings(tmp_path: Path, trigger_dir: Path) -> tuple[Settings, object]:
    database_path = tmp_path / "service-restart.db"
    sync_engine = create_engine(
        f"sqlite:///{database_path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    Base.metadata.create_all(sync_engine)
    settings = Settings(
        vision_environment="test",
        docs_enabled=False,
        database_url=SecretStr(f"sqlite+aiosqlite:///{database_path}"),
        snapshot_repository_root=tmp_path / "repos",
        snapshot_materialization_root=tmp_path / "materialized",
        snapshot_admin_service_token=SecretStr(SERVICE_TOKEN),
        snapshot_admin_identity_secret=SecretStr(IDENTITY_SECRET),
        snapshot_ops_trigger_dir=trigger_dir,
        snapshot_recovery_on_startup=False,
    )
    return settings, sync_engine


def test_admin_can_schedule_fixed_module_stack_restart_and_audit(tmp_path: Path) -> None:
    trigger_dir = tmp_path / "ops"
    trigger_dir.mkdir()
    (trigger_dir / "restart-status.json").write_text("{}", encoding="utf-8")
    settings, sync_engine = _settings(tmp_path, trigger_dir)
    app = create_app(settings)

    with TestClient(app) as client:
        status = _signed_request(
            client,
            "GET",
            "/v1/admin/runtime/services",
            role="admin",
        )
        assert status.status_code == 200
        assert status.json() == {
            "ok": True,
            "trigger_ready": True,
            "controller_state": "idle",
            "pending_scope": None,
            "in_progress": False,
            "last_execution": None,
            "scopes": ["snapshot_backend", "admin_web", "module_stack"],
            "services": ["vss-snapshot.service", "vss-admin-web.service"],
        }

        denied = _signed_request(
            client,
            "POST",
            "/v1/admin/runtime/services/restart",
            role="operator",
            payload={"scope": "module_stack"},
        )
        assert denied.status_code == 403

        scheduled = _signed_request(
            client,
            "POST",
            "/v1/admin/runtime/services/restart",
            role="admin",
            payload={"scope": "module_stack"},
        )
        assert scheduled.status_code == 202
        body = scheduled.json()
        assert body["reason"] == "MODULE_SERVICE_RESTART_SCHEDULED"
        assert body["scope"] == "module_stack"
        assert body["services"] == ["vss-snapshot.service", "vss-admin-web.service"]
        assert body["already_scheduled"] is False
        assert body["reconnect_expected"] is True

        queued = _signed_request(
            client,
            "GET",
            "/v1/admin/runtime/services",
            role="admin",
        )
        assert queued.status_code == 200
        assert queued.json()["controller_state"] == "scheduled"
        assert queued.json()["pending_scope"] == "module_stack"

    marker = trigger_dir / "restart-module-stack.request"
    assert marker.is_file()
    marker_payload = json.loads(marker.read_text(encoding="utf-8"))
    assert marker_payload["scope"] == "module_stack"
    assert marker_payload["actor"] == "kaypa"

    with Session(sync_engine) as session:
        audit = (
            session.query(AuditLog)
            .filter(AuditLog.action == "restart_module_services")
            .one()
        )
        assert audit.target_type == "module_services"
        assert audit.target_id == "module_stack"
        assert audit.outcome == "succeeded"
        assert audit.after_json["services"] == [
            "vss-snapshot.service",
            "vss-admin-web.service",
        ]
    sync_engine.dispose()


def test_admin_service_status_exposes_last_controller_execution(tmp_path: Path) -> None:
    trigger_dir = tmp_path / "ops"
    trigger_dir.mkdir()
    request_id = uuid4()
    (trigger_dir / "restart-status.json").write_text(
        json.dumps(
            {
                "state": "succeeded",
                "scope": "module_stack",
                "request_id": str(request_id),
                "actor": "kaypa",
                "scheduled_at": "2026-09-11T12:00:00Z",
                "started_at": "2026-09-11T12:00:04Z",
                "completed_at": "2026-09-11T12:00:08Z",
                "git_head": "a" * 40,
                "detail": "Restart completed and health checks passed.",
            }
        ),
        encoding="utf-8",
    )
    settings, sync_engine = _settings(tmp_path, trigger_dir)
    app = create_app(settings)

    with TestClient(app) as client:
        status = _signed_request(
            client,
            "GET",
            "/v1/admin/runtime/services",
            role="admin",
        )

    assert status.status_code == 200
    body = status.json()
    assert body["controller_state"] == "succeeded"
    assert body["pending_scope"] is None
    assert body["in_progress"] is False
    assert body["last_execution"] == {
        "state": "succeeded",
        "scope": "module_stack",
        "services": ["vss-snapshot.service", "vss-admin-web.service"],
        "request_id": str(request_id),
        "actor": "kaypa",
        "scheduled_at": "2026-09-11T12:00:00Z",
        "started_at": "2026-09-11T12:00:04Z",
        "completed_at": "2026-09-11T12:00:08Z",
        "git_head": "a" * 40,
        "detail": "Restart completed and health checks passed.",
    }
    sync_engine.dispose()


def test_restart_request_fails_closed_when_trigger_channel_is_missing(tmp_path: Path) -> None:
    trigger_dir = tmp_path / "missing-ops"
    settings, sync_engine = _settings(tmp_path, trigger_dir)
    app = create_app(settings)

    with TestClient(app) as client:
        status = _signed_request(
            client,
            "GET",
            "/v1/admin/runtime/services",
            role="admin",
        )
        assert status.status_code == 200
        assert status.json()["trigger_ready"] is False
        assert status.json()["controller_state"] == "not_configured"
        assert status.json()["last_execution"] is None

        restart = _signed_request(
            client,
            "POST",
            "/v1/admin/runtime/services/restart",
            role="admin",
            payload={"scope": "snapshot_backend"},
        )
        assert restart.status_code == 503
        assert restart.json()["reason"] == "MODULE_SERVICE_RESTART_NOT_CONFIGURED"

    sync_engine.dispose()
