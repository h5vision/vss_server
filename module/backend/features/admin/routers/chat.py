"""Administrator-only read APIs for Chat conversations and VSS response traces."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy.exc import SQLAlchemyError

from backend.core.errors import ApiError
from backend.features.admin.audit import record_audit
from backend.features.admin.common import Administrator, DbSession
from backend.features.admin.pagination import decode_cursor, paginate
from backend.features.chat_observability.maintenance import (
    ChatMaintenanceConflict,
    ChatObservabilityMaintenanceService,
    ChatRetentionPolicy,
)
from backend.features.chat_observability.schemas import (
    ChatConversationDeleteResponse,
    ChatConversationDetailResponse,
    ChatConversationListResponse,
    ChatConversationSummary,
    ChatMessageItem,
    ChatModelObservationItem,
    ChatResponseItem,
    ChatResponseTraceResponse,
    ChatRetentionPolicyResponse,
    ChatRetentionPreviewResponse,
    ChatRetentionPurgeResponse,
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


def _retention_policy(request: Request) -> ChatRetentionPolicy:
    settings = request.app.state.settings
    return ChatRetentionPolicy(
        metadata_days=settings.snapshot_chat_metadata_retention_days,
        question_answer_days=settings.snapshot_chat_question_answer_retention_days,
        full_debug_days=settings.snapshot_chat_full_debug_retention_days,
        batch_size=settings.snapshot_chat_retention_batch_size,
    )


def _maintenance_error(exc: ChatMaintenanceConflict) -> ApiError:
    return ApiError(
        status_code=exc.status_code,
        reason=exc.reason,
        detail=exc.detail,
        retryable=False,
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


@router.get("/chat/retention", response_model=ChatRetentionPreviewResponse)
async def get_chat_retention(
    request: Request,
    session: DbSession,
    _identity: Administrator,
) -> ChatRetentionPreviewResponse:
    policy = _retention_policy(request)
    try:
        preview = await ChatObservabilityMaintenanceService(session).preview_retention(policy)
    except SQLAlchemyError as exc:
        raise _database_unavailable() from exc
    return ChatRetentionPreviewResponse(
        policy=ChatRetentionPolicyResponse(
            metadata_days=policy.metadata_days,
            question_answer_days=policy.question_answer_days,
            full_debug_days=policy.full_debug_days,
            batch_size=policy.batch_size,
        ),
        eligible_total=preview.eligible_total,
        eligible_by_mode=preview.eligible_by_mode,
        evaluated_at=preview.evaluated_at,
    )


@router.delete(
    "/chat/conversations/{conversation_id}",
    response_model=ChatConversationDeleteResponse,
)
async def delete_chat_conversation(
    conversation_id: UUID,
    session: DbSession,
    identity: Administrator,
    confirm: str = Query(..., min_length=1),
) -> ChatConversationDeleteResponse:
    if confirm != str(conversation_id):
        raise ApiError(
            status_code=409,
            reason="CHAT_CONVERSATION_DELETE_CONFIRMATION_REQUIRED",
            detail="Permanent Chat deletion requires the exact conversation_id confirmation value.",
            retryable=False,
        )
    try:
        result = await ChatObservabilityMaintenanceService(session).delete_conversation(
            conversation_id
        )
    except ChatMaintenanceConflict as exc:
        raise _maintenance_error(exc) from exc
    except SQLAlchemyError as exc:
        raise _database_unavailable() from exc

    resource = {
        "conversation_id": str(result.conversation_id),
        "content_capture_mode": result.content_capture_mode,
        "project_id": result.project_id,
        "deleted_rows": result.deleted_rows,
    }
    await record_audit(
        session,
        request_id=identity.request_id,
        actor=identity.actor_id,
        action="delete_chat_conversation",
        target_type="chat_conversation",
        target_id=str(conversation_id),
        after_json=resource,
    )
    return ChatConversationDeleteResponse(
        reason="CHAT_CONVERSATION_DELETED",
        detail="The Chat conversation and its Module-owned trace history were permanently deleted.",
        request_id=identity.request_id,
        conversation_id=conversation_id,
        deleted_rows=result.deleted_rows,
    )


@router.post("/chat/retention/purge", response_model=ChatRetentionPurgeResponse)
async def purge_chat_retention(
    request: Request,
    session: DbSession,
    identity: Administrator,
    confirm: str = Query(..., min_length=1),
) -> ChatRetentionPurgeResponse:
    if confirm != "purge-expired":
        raise ApiError(
            status_code=409,
            reason="CHAT_RETENTION_PURGE_CONFIRMATION_REQUIRED",
            detail="Retention purge requires confirm=purge-expired.",
            retryable=False,
        )
    policy = _retention_policy(request)
    try:
        result = await ChatObservabilityMaintenanceService(session).purge_expired(policy)
    except SQLAlchemyError as exc:
        raise _database_unavailable() from exc

    resource = {
        "policy": {
            "metadata_days": policy.metadata_days,
            "question_answer_days": policy.question_answer_days,
            "full_debug_days": policy.full_debug_days,
            "batch_size": policy.batch_size,
        },
        "evaluated_at": result.evaluated_at.isoformat(),
        "deleted_conversations": result.deleted_conversations,
        "skipped_active": result.skipped_active,
        "deleted_rows": result.deleted_rows,
        "conversation_ids": [str(item) for item in result.conversation_ids],
    }
    await record_audit(
        session,
        request_id=identity.request_id,
        actor=identity.actor_id,
        action="purge_chat_retention",
        target_type="chat_retention",
        target_id="expired",
        after_json=resource,
    )
    return ChatRetentionPurgeResponse(
        reason="CHAT_RETENTION_PURGED",
        detail="Expired Chat observability conversations were purged according to Module policy.",
        request_id=identity.request_id,
        evaluated_at=result.evaluated_at,
        deleted_conversations=result.deleted_conversations,
        skipped_active=result.skipped_active,
        deleted_rows=result.deleted_rows,
        conversation_ids=list(result.conversation_ids),
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
