from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.app import create_app
from backend.core.config import Settings
from backend.features.admin.auth import canonical_admin_request
from backend.infrastructure.database.base import Base
from backend.infrastructure.database.models import AuditLog

SERVICE_TOKEN = "service-token-with-enough-entropy"
IDENTITY_SECRET = "identity-secret-with-at-least-32-bytes"


def _signed_request(client: TestClient, method: str, path: str):
    body = b""
    timestamp = str(int(time.time()))
    request_id = str(uuid4())
    content_sha256 = hashlib.sha256(body).hexdigest()
    canonical = canonical_admin_request(
        method=method,
        path_with_query=path,
        content_sha256=content_sha256,
        actor="operator",
        role="admin",
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
            "X-Admin-Actor": "operator",
            "X-Admin-Role": "admin",
            "X-Admin-Timestamp": timestamp,
            "X-Admin-Request-ID": request_id,
            "X-Admin-Content-SHA256": content_sha256,
            "X-Admin-Signature": signature,
        },
    )


def _database(path: Path) -> tuple[str, object]:
    db_url = f"sqlite+aiosqlite:///{path}"
    engine = create_engine(
        f"sqlite:///{path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    Base.metadata.create_all(engine)
    return db_url, engine


def test_vss_non_success_requests_are_persisted_and_visible_to_admin(tmp_path: Path) -> None:
    db_url, engine = _database(tmp_path / "vss-inbound.db")
    app = create_app(
        Settings(
            vision_environment="test",
            docs_enabled=False,
            database_url=SecretStr(db_url),
            snapshot_materialization_root=tmp_path / "snapshots",
            snapshot_repository_root=tmp_path / "repos",
            snapshot_recovery_on_startup=False,
            snapshot_vss_api_token=SecretStr("shared-vss-secret"),
            snapshot_admin_service_token=SecretStr(SERVICE_TOKEN),
            snapshot_admin_identity_secret=SecretStr(IDENTITY_SECRET),
        )
    )

    with TestClient(app) as client:
        unknown = client.get(
            "/v1/internal/vss/future-probe",
            params={"project_id": "future-project", "token": "must-not-be-stored"},
            headers={"X-Snapshot-Token": "shared-vss-secret"},
        )
        assert unknown.status_code == 404

        unauthorized = client.get(
            "/v1/internal/vss/capabilities",
            params={"project_id": "auth-probe"},
        )
        assert unauthorized.status_code == 401

        healthy = client.get(
            "/v1/internal/vss/capabilities",
            headers={"X-Snapshot-Token": "shared-vss-secret"},
        )
        assert healthy.status_code == 200

        visible = _signed_request(
            client,
            "GET",
            "/v1/admin/vss/request-failures?limit=100",
        )
        assert visible.status_code == 200
        payload = visible.json()
        assert len(payload["items"]) == 2
        assert {item["status_code"] for item in payload["items"]} == {401, 404}
        assert {item["path"] for item in payload["items"]} == {
            "/v1/internal/vss/future-probe",
            "/v1/internal/vss/capabilities",
        }
        probe = next(item for item in payload["items"] if item["status_code"] == 404)
        assert probe["project_id"] == "future-project"
        assert probe["query"]["token"] == "<redacted>"

    with Session(engine) as session:
        rows = list(
            session.scalars(
                select(AuditLog)
                .where(AuditLog.action == "vss_inbound_request")
                .order_by(AuditLog.created_at)
            )
        )
    assert len(rows) == 2
    assert all(row.actor == "vss-inbound" for row in rows)
    assert {row.reason for row in rows} == {"HTTP_401", "HTTP_404"}
    assert all(row.details and "method" in row.details for row in rows)
    assert "must-not-be-stored" not in json.dumps([row.details for row in rows])
    engine.dispose()
