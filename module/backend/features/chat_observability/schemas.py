"""Read models for Chat conversations and response traces exposed to Admin."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ChatConversationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    title: str | None
    requester_type: Literal["authenticated_user", "client_instance", "admin", "internal", "unknown"]
    requester_id: str | None
    client_instance_id: str | None
    origin: Literal["vision_frontend", "admin_debug", "api", "internal_test", "unknown"]
    project_id: str | None
    content_capture_mode: Literal["metadata", "question_answer", "full_debug"]
    status: Literal["active", "archived"]
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None
    message_count: int = 0
    response_count: int = 0
    last_response_status: str | None = None
    last_chat_model: str | None = None
    last_embedding_model: str | None = None


class ChatConversationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ChatConversationSummary]
    next_cursor: str | None = None


class ChatMessageItem(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    message_id: UUID
    conversation_id: UUID
    sequence: int
    role: Literal["user", "assistant", "system"]
    content: str | None
    content_sha256: str | None
    content_length: int
    requester_id: str | None
    created_at: datetime


class ChatResponseItem(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    response_id: UUID
    trace_id: UUID
    conversation_id: UUID
    user_message_id: UUID
    assistant_message_id: UUID | None
    vss_request_id: str
    project_id: str | None
    index_id: str | None
    resolved_by: str | None
    chat_model: str | None
    embedding_model: str | None
    status: Literal["created", "running", "completed", "failed", "cancelled"]
    outcome: Literal["answered", "no_evidence", "error"] | None
    has_evidence: bool | None
    top_score: float | None
    threshold: float | None
    source_count: int
    reference_count: int
    embed_ms: float | None
    search_ms: float | None
    bm25_ms: float | None
    symbol_ms: float | None
    prompt_ms: float | None
    pre_llm_ms: float | None
    ttft_ms: float | None
    gen_ms: float | None
    total_ms: float | None
    decode_tok_s: float | None
    eval_count: int | None
    error_code: str | None
    error_detail: str | None
    started_at: datetime
    first_token_at: datetime | None
    completed_at: datetime | None


class ChatConversationDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation: ChatConversationSummary
    messages: list[ChatMessageItem]
    responses: list[ChatResponseItem]


class ChatTraceEventItem(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    event_id: UUID
    trace_id: UUID
    sequence: int
    event_type: Literal[
        "request", "runtime_snapshot", "meta", "stage", "delta_batch", "done", "error"
    ]
    elapsed_ms: float | None
    payload: dict[str, Any]
    created_at: datetime


class ChatModelObservationItem(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    observation_id: UUID
    trace_id: UUID
    role: Literal["embedding", "completion"]
    model_name: str
    stage: str
    runtime_available: bool
    resident: bool | None
    observed_at: datetime


class ChatResponseTraceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response: ChatResponseItem
    events: list[ChatTraceEventItem]
    model_observations: list[ChatModelObservationItem]


class ChatRetentionPolicyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metadata_days: int
    question_answer_days: int
    full_debug_days: int
    batch_size: int


class ChatRetentionPreviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: ChatRetentionPolicyResponse
    eligible_total: int
    eligible_by_mode: dict[str, int]
    evaluated_at: datetime


class ChatConversationDeleteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Literal["CHAT_CONVERSATION_DELETED"]
    detail: str
    request_id: UUID
    conversation_id: UUID
    deleted_rows: dict[str, int]


class ChatRetentionPurgeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Literal["CHAT_RETENTION_PURGED"]
    detail: str
    request_id: UUID
    evaluated_at: datetime
    deleted_conversations: int
    skipped_active: int
    deleted_rows: dict[str, int]
    conversation_ids: list[UUID]
