from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.infrastructure.database.base import Base
from backend.infrastructure.database.models import (
    ChatConversation,
    ChatMessage,
    ChatModelObservation,
    ChatResponse,
    ChatTraceEvent,
)


@pytest.fixture
def db_session() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(engine)


def _conversation() -> ChatConversation:
    return ChatConversation(
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


def test_chat_trace_graph_persists_exact_response_correlation(db_session: Session) -> None:
    conversation = _conversation()
    db_session.add(conversation)
    db_session.flush()

    user_message = ChatMessage(
        conversation_id=conversation.conversation_id,
        sequence=1,
        role="user",
        content="증분 인덱싱은 어떻게 동작해?",
        content_sha256="a" * 64,
        content_length=18,
        requester_id="kaypa",
    )
    db_session.add(user_message)
    db_session.flush()

    trace_id = uuid.uuid4()
    response = ChatResponse(
        trace_id=trace_id,
        conversation_id=conversation.conversation_id,
        user_message_id=user_message.message_id,
        vss_request_id=str(trace_id),
        project_id="vss_server-pre-rag",
        index_id="vss_server-pre-rag",
        resolved_by="exact",
        chat_model="qwen3:27b",
        embedding_model="bge-m3:latest",
        status="running",
        has_evidence=True,
        source_count=3,
        reference_count=2,
        embed_ms=41.2,
        search_ms=12.4,
        ttft_ms=612.1,
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(response)
    db_session.flush()

    assistant_message = ChatMessage(
        conversation_id=conversation.conversation_id,
        sequence=2,
        role="assistant",
        content="변경된 파일만 다시 임베딩합니다.",
        content_sha256="b" * 64,
        content_length=20,
    )
    db_session.add(assistant_message)
    db_session.flush()
    response.assistant_message_id = assistant_message.message_id
    response.status = "completed"
    response.outcome = "answered"
    response.completed_at = datetime.now(timezone.utc)

    db_session.add_all(
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
                payload={"has_evidence": True},
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
    db_session.commit()

    saved = db_session.scalar(select(ChatResponse).where(ChatResponse.trace_id == trace_id))
    assert saved is not None
    assert saved.vss_request_id == str(trace_id)
    assert saved.assistant_message_id == assistant_message.message_id
    assert saved.status == "completed"
    assert saved.outcome == "answered"
    assert len(db_session.scalars(select(ChatTraceEvent)).all()) == 2
    assert len(db_session.scalars(select(ChatModelObservation)).all()) == 2


def test_chat_trace_event_sequence_is_unique_per_trace(db_session: Session) -> None:
    conversation = _conversation()
    db_session.add(conversation)
    db_session.flush()
    user_message = ChatMessage(
        conversation_id=conversation.conversation_id,
        sequence=1,
        role="user",
        content=None,
        content_sha256="c" * 64,
        content_length=42,
    )
    db_session.add(user_message)
    db_session.flush()
    trace_id = uuid.uuid4()
    db_session.add(
        ChatResponse(
            trace_id=trace_id,
            conversation_id=conversation.conversation_id,
            user_message_id=user_message.message_id,
            vss_request_id=str(trace_id),
            status="created",
        )
    )
    db_session.flush()
    db_session.add_all(
        [
            ChatTraceEvent(
                trace_id=trace_id,
                sequence=1,
                event_type="request",
                payload={},
            ),
            ChatTraceEvent(
                trace_id=trace_id,
                sequence=1,
                event_type="stage",
                payload={},
            ),
        ]
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_chat_model_rejects_invalid_identity_and_capture_values(db_session: Session) -> None:
    db_session.add(
        ChatConversation(
            requester_type="ip_guess",
            origin="vision_frontend",
            content_capture_mode="everything_forever",
            status="active",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
