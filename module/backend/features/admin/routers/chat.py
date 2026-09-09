"""Administrator-only read APIs for Chat conversations and VSS response traces."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy.exc import SQLAlchemyError

from backend.core.errors import ApiError
from backend.features.admin.common import Administrator, DbSession
from backend.features.admin.pagination import decode_cursor, paginate
from backend.features.chat_observability.schemas import (
    ChatConversationDetailResponse,
    ChatConversationListResponse,
    ChatConversationSummary,
    ChatMessageItem,
    ChatModelObservationItem,
    ChatResponseItem,
    ChatResponseTraceResponse,
    ChatTraceEventItem,
)
from backend.features.chat_observability.store import (
    ChatObservabilityStore,
    ConversationListRow,
)

router = APIRouter()


def _database_unavailable() -> ApiError:
    return ApiError(
        status_code=503,
        reason="DATABASE_UNAVAILABLE",
        detail="Chat observability data is temporarily unavailable.",
        retryable=True,
    )


def _summary(row: ConversationListRow) -> ChatConversationSummary:
    conversation = row.conversation
    return ChatConversationSummary(
        conversation_id=conversation.conversation_id,
        title=conversation.title,
        requester_type=conversation.requester_type,
        requester_id=conversation.requester_id,
        client_instance_id=conversation.client_instance_id,
        origin=conversation.origin,
        project_id=conversation.project_id,
        content_capture_mode=conversation.content_capture_mode,
        status=conversation.status,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        last_message_at=conversation.last_message_at,
        message_count=row.message_count,
        response_count=row.response_count,
        last_response_status=row.last_response_status,
        last_chat_model=row.last_chat_model,
        last_embedding_model=row.last_embedding_model,
    )


def _detail_summary(row) -> ChatConversationSummary:
    return ChatConversationSummary(
        conversation_id=row.conversation.conversation_id,
        title=row.conversation.title,
        requester_type=row.conversation.requester_type,
        requester_id=row.conversation.requester_id,
        client_instance_id=row.conversation.client_instance_id,
        origin=row.conversation.origin,
        project_id=row.conversation.project_id,
        content_capture_mode=row.conversation.content_capture_mode,
        status=row.conversation.status,
        created_at=row.conversation.created_at,
        updated_at=row.conversation.updated_at,
        last_message_at=row.conversation.last_message_at,
        message_count=len(row.messages),
        response_count=len(row.responses),
        last_response_status=row.responses[-1].status if row.responses else None,
        last_chat_model=row.responses[-1].chat_model if row.responses else None,
        last_embedding_model=row.responses[-1].embedding_model if row.responses else None,
    )


@router.get("/chat/conversations", response_model=ChatConversationListResponse)
async def list_chat_conversations(
    session: DbSession,
    _identity: Administrator,
    project_id: str | None = Query(default=None, min_length=1),
    requester_id: str | None = Query(default=None, min_length=1),
    status: str | None = Query(default=None, pattern="^(active|archived)$"),
    chat_model: str | None = Query(default=None, min_length=1),
    embedding_model: str | None = Query(default=None, min_length=1),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = None,
) -> ChatConversationListResponse:
    offset = decode_cursor(cursor)
    try:
        rows = await ChatObservabilityStore(session).list_conversations(
            limit=limit + 1,
            offset=offset,
            project_id=project_id,
            requester_id=requester_id,
            status=status,
            chat_model=chat_model,
            embedding_model=embedding_model,
        )
    except SQLAlchemyError as exc:
        raise _database_unavailable() from exc
    visible, next_cursor = paginate(rows, limit=limit, offset=offset)
    return ChatConversationListResponse(
        items=[_summary(row) for row in visible],
        next_cursor=next_cursor,
    )


@router.get(
    "/chat/conversations/{conversation_id}",
    response_model=ChatConversationDetailResponse,
)
async def get_chat_conversation(
    conversation_id: UUID,
    session: DbSession,
    _identity: Administrator,
) -> ChatConversationDetailResponse:
    try:
        row = await ChatObservabilityStore(session).get_conversation(conversation_id)
    except SQLAlchemyError as exc:
        raise _database_unavailable() from exc
    if row is None:
        raise ApiError(
            status_code=404,
            reason="CHAT_CONVERSATION_NOT_FOUND",
            detail="The requested Chat conversation was not found.",
            retryable=False,
        )
    return ChatConversationDetailResponse(
        conversation=_detail_summary(row),
        messages=[ChatMessageItem.model_validate(item) for item in row.messages],
        responses=[ChatResponseItem.model_validate(item) for item in row.responses],
    )


@router.get(
    "/chat/responses/{response_id}/trace",
    response_model=ChatResponseTraceResponse,
)
async def get_chat_response_trace(
    response_id: UUID,
    session: DbSession,
    _identity: Administrator,
) -> ChatResponseTraceResponse:
    try:
        row = await ChatObservabilityStore(session).get_response_trace(response_id)
    except SQLAlchemyError as exc:
        raise _database_unavailable() from exc
    if row is None:
        raise ApiError(
            status_code=404,
            reason="CHAT_RESPONSE_NOT_FOUND",
            detail="The requested Chat response trace was not found.",
            retryable=False,
        )
    return ChatResponseTraceResponse(
        response=ChatResponseItem.model_validate(row.response),
        events=[ChatTraceEventItem.model_validate(item) for item in row.events],
        model_observations=[
            ChatModelObservationItem.model_validate(item) for item in row.model_observations
        ],
    )
