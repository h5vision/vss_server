from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from backend.app import create_app
from backend.core.config import Settings
from backend.features.admin.auth import canonical_admin_request
from backend.infrastructure.database.base import Base
from backend.infrastructure.database.models import (
    AuditLog,
    ChatConversation,
    ChatMessage,
    ChatModelObservation,
    ChatResponse,
    ChatTraceEvent,
)

SERVICE_TOKEN = "service-token-with-enough-entropy"
IDENTITY_SECRET = "identity-secret-with-at-least-32-bytes"


def _signed_request(
    client: TestClient,
    method: str,
    path: str,
    *,
    role: str = "admin",
):
    body = b""
    timestamp = str(int(time.time()))
    request_id = str(uuid.uuid4())
    digest = hashlib.sha256(body).hexdigest()
    canonical = canonical_admin_request(
        method=method,
        path_with_query=path,
        content_sha256=digest,
        actor="trace-admin",
        role=role,
        timestamp=timestamp,
        request_id=request_id,
    )
    signature = hmac.new(IDENTITY_SECRET.encode(), canonical, hashlib.sha256).hexdigest()
    return client.request(
        method,
        path,
        headers={
            "Authorization": f"Bearer {SERVICE_TOKEN}",
            "X-Admin-Actor": "trace-admin",
            "X-Admin-Role": role,
            "X-Admin-Timestamp": timestamp,
            "X-Admin-Request-ID": request_id,
            "X-Admin-Content-SHA256": digest,
            "X-Admin-Signature": signature,
        },
    )


def _settings(database_path: Path, tmp_path: Path, **overrides) -> Settings:
    values = {
        "vision_environment": "test",
        "docs_enabled": False,
        "database_url": SecretStr(f"sqlite+aiosqlite:///{database_path}"),
        "snapshot_materialization_root": tmp_path / "snapshots",
        "snapshot_repository_root": tmp_path / "repos",
        "snapshot_recovery_on_startup": False,
        "snapshot_admin_service_token": SecretStr(SERVICE_TOKEN),
        "snapshot_admin_identity_secret": SecretStr(IDENTITY_SECRET),
    }
    values.update(overrides)
    return Settings(**values)


def _seed_conversation(
    engine,
    *,
    capture_mode: str = "question_answer",
    age_days: int = 0,
    response_status: str = "completed",
) -> uuid.UUID:
    observed_at = datetime.now(timezone.utc) - timedelta(days=age_days)
    with Session(engine) as session:
        conversation = ChatConversation(
            title=f"{capture_mode}-{age_days}",
            requester_type="client_instance",
            client_instance_id=f"client-{capture_mode}-{age_days}-{response_status}",
            origin="vision_frontend",
            project_id="maintenance-project",
            content_capture_mode=capture_mode,
            status="active",
            last_message_at=observed_at,
        )
        session.add(conversation)
        session.flush()
        user_message = ChatMessage(
            conversation_id=conversation.conversation_id,
            sequence=1,
            role="user",
            content="question" if capture_mode != "metadata" else None,
            content_sha256="a" * 64,
            content_length=8,
            created_at=observed_at,
        )
        session.add(user_message)
        session.flush()

        assistant_message = None
        if response_status == "completed":
            assistant_message = ChatMessage(
                conversation_id=conversation.conversation_id,
                sequence=2,
                role="assistant",
                content="answer" if capture_mode != "metadata" else None,
                content_sha256="b" * 64,
                content_length=6,
                created_at=observed_at,
            )
            session.add(assistant_message)
            session.flush()

        trace_id = uuid.uuid4()
        response = ChatResponse(
            trace_id=trace_id,
            conversation_id=conversation.conversation_id,
            user_message_id=user_message.message_id,
            assistant_message_id=(
                assistant_message.message_id if assistant_message is not None else None
            ),
            vss_request_id=str(trace_id),
            project_id="maintenance-project",
            chat_model="qwen3.8:27b",
            embedding_model="bge-m3:latest",
            status=response_status,
            outcome="answered" if response_status == "completed" else None,
            started_at=observed_at,
            completed_at=observed_at if response_status == "completed" else None,
        )
        session.add(response)
        session.flush()
        session.add_all(
            [
                ChatTraceEvent(
                    trace_id=trace_id,
                    sequence=1,
                    event_type="request",
                    payload={"origin": "vision_frontend"},
                    created_at=observed_at,
                ),
                ChatModelObservation(
                    trace_id=trace_id,
                    role="completion",
                    model_name="qwen3.8:27b",
                    stage="generation",
                    runtime_available=True,
                    resident=True,
                    observed_at=observed_at,
                ),
            ]
        )
        session.commit()
        return conversation.conversation_id


def _engine(database_path: Path):
    engine = create_engine(
        f"sqlite:///{database_path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    Base.metadata.create_all(engine)
    return engine


def test_admin_can_delete_completed_chat_conversation_with_exact_confirmation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "delete-chat.db"
    engine = _engine(database_path)
    conversation_id = _seed_conversation(engine)
    app = create_app(_settings(database_path, tmp_path))

    with TestClient(app) as client:
        wrong = _signed_request(
            client,
            "DELETE",
            f"/v1/admin/chat/conversations/{conversation_id}?confirm=wrong",
        )
        assert wrong.status_code == 409
        assert wrong.json()["reason"] == "CHAT_CONVERSATION_DELETE_CONFIRMATION_REQUIRED"

        path = (
            f"/v1/admin/chat/conversations/{conversation_id}"
            f"?confirm={conversation_id}"
        )
        deleted = _signed_request(client, "DELETE", path)
        assert deleted.status_code == 200
        payload = deleted.json()
        assert payload["conversation_id"] == str(conversation_id)
        assert payload["deleted_rows"] == {
            "chat_conversations": 1,
            "chat_messages": 2,
            "chat_responses": 1,
            "chat_trace_events": 1,
            "chat_model_observations": 1,
        }

    with Session(engine) as session:
        assert session.get(ChatConversation, conversation_id) is None
        assert session.scalar(select(func.count(ChatMessage.message_id))) == 0
        assert session.scalar(select(func.count(ChatResponse.response_id))) == 0
        assert session.scalar(select(func.count(ChatTraceEvent.event_id))) == 0
        assert session.scalar(select(func.count(ChatModelObservation.observation_id))) == 0
        audit = session.scalar(
            select(AuditLog).where(AuditLog.action == "delete_chat_conversation")
        )
        assert audit is not None
        assert audit.target_id == str(conversation_id)
        assert audit.after_json["deleted_rows"]["chat_messages"] == 2

    engine.dispose()


def test_admin_cannot_delete_conversation_with_running_response(tmp_path: Path) -> None:
    database_path = tmp_path / "active-chat.db"
    engine = _engine(database_path)
    conversation_id = _seed_conversation(engine, response_status="running")
    app = create_app(_settings(database_path, tmp_path))

    with TestClient(app) as client:
        path = (
            f"/v1/admin/chat/conversations/{conversation_id}"
            f"?confirm={conversation_id}"
        )
        response = _signed_request(client, "DELETE", path)
        assert response.status_code == 409
        assert response.json()["reason"] == "CHAT_CONVERSATION_ACTIVE"

    with Session(engine) as session:
        assert session.get(ChatConversation, conversation_id) is not None
        assert session.scalar(select(func.count(ChatResponse.response_id))) == 1

    engine.dispose()


def test_retention_preview_and_explicit_purge_respect_capture_mode_and_active_guard(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "retention-chat.db"
    engine = _engine(database_path)
    expired = {
        _seed_conversation(engine, capture_mode="metadata", age_days=91),
        _seed_conversation(engine, capture_mode="question_answer", age_days=31),
        _seed_conversation(engine, capture_mode="full_debug", age_days=4),
    }
    recent_id = _seed_conversation(engine, capture_mode="question_answer", age_days=10)
    active_id = _seed_conversation(
        engine,
        capture_mode="full_debug",
        age_days=10,
        response_status="running",
    )
    app = create_app(
        _settings(
            database_path,
            tmp_path,
            snapshot_chat_metadata_retention_days=90,
            snapshot_chat_question_answer_retention_days=30,
            snapshot_chat_full_debug_retention_days=3,
            snapshot_chat_retention_batch_size=10,
        )
    )

    with TestClient(app) as client:
        preview = _signed_request(client, "GET", "/v1/admin/chat/retention")
        assert preview.status_code == 200
        preview_payload = preview.json()
        assert preview_payload["eligible_total"] == 3
        assert preview_payload["eligible_by_mode"] == {
            "metadata": 1,
            "question_answer": 1,
            "full_debug": 1,
        }
        assert preview_payload["policy"]["batch_size"] == 10

        wrong = _signed_request(
            client,
            "POST",
            "/v1/admin/chat/retention/purge?confirm=wrong",
        )
        assert wrong.status_code == 409

        purged = _signed_request(
            client,
            "POST",
            "/v1/admin/chat/retention/purge?confirm=purge-expired",
        )
        assert purged.status_code == 200
        purge_payload = purged.json()
        assert purge_payload["deleted_conversations"] == 3
        assert purge_payload["skipped_active"] == 0
        assert set(purge_payload["conversation_ids"]) == {str(item) for item in expired}

    with Session(engine) as session:
        remaining = set(session.scalars(select(ChatConversation.conversation_id)).all())
        assert remaining == {recent_id, active_id}
        audit = session.scalar(select(AuditLog).where(AuditLog.action == "purge_chat_retention"))
        assert audit is not None
        assert audit.after_json["deleted_conversations"] == 3

    engine.dispose()
