from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app import create_app
from backend.core.config import Settings
from backend.features.admin.auth import canonical_admin_request
from backend.infrastructure.database.base import Base
from backend.infrastructure.database.models import (
    ChatConversation,
    ChatMessage,
    ChatModelObservation,
    ChatResponse,
    ChatTraceEvent,
)

SERVICE_TOKEN = "service-token-with-enough-entropy"
IDENTITY_SECRET = "identity-secret-with-at-least-32-bytes"


def _signed_get(client: TestClient, path: str, *, role: str = "admin"):
    body = b""
    timestamp = str(int(time.time()))
    request_id = str(uuid.uuid4())
    digest = hashlib.sha256(body).hexdigest()
    canonical = canonical_admin_request(
        method="GET",
        path_with_query=path,
        content_sha256=digest,
        actor="trace-admin",
        role=role,
        timestamp=timestamp,
        request_id=request_id,
    )
    signature = hmac.new(IDENTITY_SECRET.encode(), canonical, hashlib.sha256).hexdigest()
    return client.get(
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


def _seed(engine) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    with Session(engine) as session:
        conversation = ChatConversation(
            title="VSS indexing 질문",
            requester_type="authenticated_user",
            requester_id="kaypa",
            client_instance_id="vision-win-01",
            origin="vision_frontend",
            project_id="vss_server-pre-rag",
            content_capture_mode="question_answer",
            status="active",
            last_message_at=datetime.now(timezone.utc),
        )
        session.add(conversation)
        session.flush()
        user_message = ChatMessage(
            conversation_id=conversation.conversation_id,
            sequence=1,
            role="user",
            content="증분 인덱싱은 어떻게 동작해?",
            content_sha256="a" * 64,
            content_length=18,
            requester_id="kaypa",
        )
        assistant_message = ChatMessage(
            conversation_id=conversation.conversation_id,
            sequence=2,
            role="assistant",
            content="변경된 파일만 다시 임베딩합니다.",
            content_sha256="b" * 64,
            content_length=20,
        )
        session.add_all([user_message, assistant_message])
        session.flush()
        trace_id = uuid.uuid4()
        response = ChatResponse(
            trace_id=trace_id,
            conversation_id=conversation.conversation_id,
            user_message_id=user_message.message_id,
            assistant_message_id=assistant_message.message_id,
            vss_request_id=str(trace_id),
            project_id="vss_server-pre-rag",
            index_id="vss_server-pre-rag",
            resolved_by="exact",
            chat_model="qwen3:27b",
            embedding_model="bge-m3:latest",
            status="completed",
            outcome="answered",
            has_evidence=True,
            source_count=3,
            reference_count=2,
            embed_ms=41.2,
            search_ms=12.4,
            ttft_ms=612.1,
            gen_ms=2810.0,
            total_ms=3122.0,
            decode_tok_s=50.2,
            eval_count=142,
            completed_at=datetime.now(timezone.utc),
        )
        session.add(response)
        session.flush()
        session.add_all(
            [
                ChatTraceEvent(
                    trace_id=trace_id,
                    sequence=1,
                    event_type="request",
                    elapsed_ms=0.0,
                    payload={"origin": "vision_frontend"},
                ),
                ChatTraceEvent(
                    trace_id=trace_id,
                    sequence=2,
                    event_type="meta",
                    elapsed_ms=65.0,
                    payload={"has_evidence": True, "source_count": 3},
                ),
                ChatModelObservation(
                    trace_id=trace_id,
                    role="embedding",
                    model_name="bge-m3:latest",
                    stage="retrieval",
                    runtime_available=True,
                    resident=True,
                ),
                ChatModelObservation(
                    trace_id=trace_id,
                    role="completion",
                    model_name="qwen3:27b",
                    stage="generation",
                    runtime_available=True,
                    resident=True,
                ),
            ]
        )
        session.commit()
        return conversation.conversation_id, response.response_id, trace_id


def test_admin_can_browse_conversation_transcript_and_response_trace(tmp_path: Path) -> None:
    database_path = tmp_path / "chat-observability.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    Base.metadata.create_all(engine)
    conversation_id, response_id, trace_id = _seed(engine)
    app = create_app(
        Settings(
            vision_environment="test",
            docs_enabled=False,
            database_url=SecretStr(f"sqlite+aiosqlite:///{database_path}"),
            snapshot_materialization_root=tmp_path / "snapshots",
            snapshot_repository_root=tmp_path / "repos",
            snapshot_recovery_on_startup=False,
            snapshot_admin_service_token=SecretStr(SERVICE_TOKEN),
            snapshot_admin_identity_secret=SecretStr(IDENTITY_SECRET),
        )
    )

    with TestClient(app) as client:
        list_path = "/v1/admin/chat/conversations?limit=50&project_id=vss_server-pre-rag"
        listed = _signed_get(client, list_path)
        assert listed.status_code == 200
        assert len(listed.json()["items"]) == 1
        item = listed.json()["items"][0]
        assert item["conversation_id"] == str(conversation_id)
        assert item["requester_id"] == "kaypa"
        assert item["message_count"] == 2
        assert item["response_count"] == 1
        assert item["last_response_status"] == "completed"
        assert item["last_chat_model"] == "qwen3:27b"
        assert item["last_embedding_model"] == "bge-m3:latest"

        by_model = _signed_get(
            client,
            "/v1/admin/chat/conversations?limit=50&chat_model=qwen3%3A27b",
        )
        assert by_model.status_code == 200
        assert [row["conversation_id"] for row in by_model.json()["items"]] == [
            str(conversation_id)
        ]
        wrong_model = _signed_get(
            client,
            "/v1/admin/chat/conversations?limit=50&chat_model=other-model",
        )
        assert wrong_model.status_code == 200
        assert wrong_model.json()["items"] == []

        detail_path = f"/v1/admin/chat/conversations/{conversation_id}"
        detail = _signed_get(client, detail_path)
        assert detail.status_code == 200
        payload = detail.json()
        assert [message["role"] for message in payload["messages"]] == ["user", "assistant"]
        assert payload["messages"][0]["content"] == "증분 인덱싱은 어떻게 동작해?"
        assert payload["responses"][0]["trace_id"] == str(trace_id)
        assert payload["responses"][0]["chat_model"] == "qwen3:27b"
        assert payload["responses"][0]["embedding_model"] == "bge-m3:latest"

        trace_path = f"/v1/admin/chat/responses/{response_id}/trace"
        trace = _signed_get(client, trace_path)
        assert trace.status_code == 200
        trace_payload = trace.json()
        assert [event["sequence"] for event in trace_payload["events"]] == [1, 2]
        assert {item["role"] for item in trace_payload["model_observations"]} == {
            "embedding",
            "completion",
        }
        assert trace_payload["response"]["ttft_ms"] == 612.1
        assert trace_payload["response"]["decode_tok_s"] == 50.2

        denied = _signed_get(client, list_path, role="viewer")
        assert denied.status_code == 403

    engine.dispose()
