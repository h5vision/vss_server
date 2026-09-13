"""ORM persistence for Chat conversations, responses, and observability traces."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.infrastructure.database.base import Base


class ChatConversation(Base):
    __tablename__ = "chat_conversations"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    requester_type: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    requester_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_instance_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    origin: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    project_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_capture_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="metadata"
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "requester_type IN ('authenticated_user','client_instance','admin',"
            "'internal','unknown')",
            name="ck_chat_conversations_requester_type",
        ),
        CheckConstraint(
            "origin IN ('vision_frontend','admin_debug','api','internal_test','unknown')",
            name="ck_chat_conversations_origin",
        ),
        CheckConstraint(
            "content_capture_mode IN ('metadata','question_answer','full_debug')",
            name="ck_chat_conversations_capture_mode",
        ),
        CheckConstraint(
            "status IN ('active','archived')",
            name="ck_chat_conversations_status",
        ),
        Index("ix_chat_conversations_last_message", "last_message_at"),
        Index("ix_chat_conversations_requester", "requester_type", "requester_id"),
        Index("ix_chat_conversations_project", "project_id", "last_message_at"),
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    message_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("chat_conversations.conversation_id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_length: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requester_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "sequence", name="uq_chat_messages_conversation_sequence"
        ),
        CheckConstraint("sequence >= 0", name="ck_chat_messages_sequence"),
        CheckConstraint("role IN ('user','assistant','system')", name="ck_chat_messages_role"),
        CheckConstraint("content_length >= 0", name="ck_chat_messages_content_length"),
        CheckConstraint(
            "content_sha256 IS NULL OR length(content_sha256) = 64",
            name="ck_chat_messages_content_sha256",
        ),
        Index("ix_chat_messages_conversation_created", "conversation_id", "created_at"),
    )


class ChatResponse(Base):
    __tablename__ = "chat_responses"

    response_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    trace_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("chat_conversations.conversation_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_message_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("chat_messages.message_id", ondelete="RESTRICT"),
        nullable=False,
    )
    assistant_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("chat_messages.message_id", ondelete="SET NULL"),
        nullable=True,
    )
    vss_request_id: Mapped[str] = mapped_column(String(255), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    index_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chat_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created")
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    has_evidence: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    top_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reference_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embed_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    search_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    bm25_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    symbol_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    prompt_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    pre_llm_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    ttft_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    gen_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    decode_tok_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    eval_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    first_token_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("trace_id", name="uq_chat_responses_trace_id"),
        UniqueConstraint("vss_request_id", name="uq_chat_responses_vss_request_id"),
        CheckConstraint(
            "status IN ('created','running','completed','failed','cancelled')",
            name="ck_chat_responses_status",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('answered','no_evidence','error')",
            name="ck_chat_responses_outcome",
        ),
        CheckConstraint("source_count >= 0", name="ck_chat_responses_source_count"),
        CheckConstraint("reference_count >= 0", name="ck_chat_responses_reference_count"),
        CheckConstraint(
            "eval_count IS NULL OR eval_count >= 0", name="ck_chat_responses_eval_count"
        ),
        Index("ix_chat_responses_conversation_started", "conversation_id", "started_at"),
        Index("ix_chat_responses_status_started", "status", "started_at"),
        Index("ix_chat_responses_chat_model", "chat_model", "started_at"),
        Index("ix_chat_responses_embedding_model", "embedding_model", "started_at"),
    )


class ChatTraceEvent(Base):
    __tablename__ = "chat_trace_events"

    event_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    trace_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("chat_responses.trace_id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    elapsed_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("trace_id", "sequence", name="uq_chat_trace_events_trace_sequence"),
        CheckConstraint("sequence >= 0", name="ck_chat_trace_events_sequence"),
        CheckConstraint(
            "event_type IN ('request','runtime_snapshot','meta','stage','delta_batch',"
            "'done','error')",
            name="ck_chat_trace_events_type",
        ),
        CheckConstraint(
            "elapsed_ms IS NULL OR elapsed_ms >= 0", name="ck_chat_trace_events_elapsed_ms"
        ),
        Index("ix_chat_trace_events_trace_created", "trace_id", "created_at"),
    )


class ChatModelObservation(Base):
    __tablename__ = "chat_model_observations"

    observation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    trace_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("chat_responses.trace_id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_available: Mapped[bool] = mapped_column(Boolean, nullable=False)
    resident: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "role IN ('embedding','completion')", name="ck_chat_model_observations_role"
        ),
        Index("ix_chat_model_observations_trace_observed", "trace_id", "observed_at"),
        Index("ix_chat_model_observations_model_observed", "model_name", "observed_at"),
    )
