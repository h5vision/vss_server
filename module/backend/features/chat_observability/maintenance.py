"""Administrator-triggered retention and deletion for Chat observability data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.infrastructure.database.models import (
    ChatConversation,
    ChatMessage,
    ChatModelObservation,
    ChatResponse,
    ChatTraceEvent,
)

_ACTIVE_RESPONSE_STATUSES = ("created", "running")
_CAPTURE_MODES = ("metadata", "question_answer", "full_debug")


class ChatMaintenanceConflict(RuntimeError):
    def __init__(self, *, reason: str, detail: str, status_code: int = 409) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ChatRetentionPolicy:
    metadata_days: int
    question_answer_days: int
    full_debug_days: int
    batch_size: int

    def days_for(self, capture_mode: str) -> int:
        if capture_mode == "metadata":
            return self.metadata_days
        if capture_mode == "question_answer":
            return self.question_answer_days
        if capture_mode == "full_debug":
            return self.full_debug_days
        raise ValueError(f"Unsupported capture mode: {capture_mode}")


@dataclass(frozen=True, slots=True)
class ChatRetentionPreview:
    eligible_total: int
    eligible_by_mode: dict[str, int]
    evaluated_at: datetime


@dataclass(frozen=True, slots=True)
class ChatDeleteResult:
    conversation_id: UUID
    content_capture_mode: str
    project_id: str | None
    deleted_rows: dict[str, int]


@dataclass(frozen=True, slots=True)
class ChatRetentionPurgeResult:
    deleted_conversations: int
    skipped_active: int
    deleted_rows: dict[str, int]
    conversation_ids: tuple[UUID, ...]
    evaluated_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _conversation_timestamp():
    return func.coalesce(ChatConversation.last_message_at, ChatConversation.created_at)


class ChatObservabilityMaintenanceService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _active_response_exists():
        return (
            select(ChatResponse.response_id)
            .where(
                ChatResponse.conversation_id == ChatConversation.conversation_id,
                ChatResponse.status.in_(_ACTIVE_RESPONSE_STATUSES),
            )
            .exists()
        )

    @staticmethod
    def _expired_condition(policy: ChatRetentionPolicy, evaluated_at: datetime):
        timestamp = _conversation_timestamp()
        return or_(
            *[
                (
                    (ChatConversation.content_capture_mode == capture_mode)
                    & (timestamp < evaluated_at - timedelta(days=policy.days_for(capture_mode)))
                )
                for capture_mode in _CAPTURE_MODES
            ]
        )

    async def preview_retention(
        self,
        policy: ChatRetentionPolicy,
        *,
        evaluated_at: datetime | None = None,
    ) -> ChatRetentionPreview:
        evaluated = evaluated_at or _now()
        active_exists = self._active_response_exists()
        counts: dict[str, int] = {}
        timestamp = _conversation_timestamp()
        for capture_mode in _CAPTURE_MODES:
            cutoff = evaluated - timedelta(days=policy.days_for(capture_mode))
            count = await self._session.scalar(
                select(func.count(ChatConversation.conversation_id)).where(
                    ChatConversation.content_capture_mode == capture_mode,
                    timestamp < cutoff,
                    ~active_exists,
                )
            )
            counts[capture_mode] = int(count or 0)
        return ChatRetentionPreview(
            eligible_total=sum(counts.values()),
            eligible_by_mode=counts,
            evaluated_at=evaluated,
        )

    async def _candidate_ids(
        self,
        policy: ChatRetentionPolicy,
        *,
        evaluated_at: datetime,
    ) -> tuple[UUID, ...]:
        active_exists = self._active_response_exists()
        rows = await self._session.scalars(
            select(ChatConversation.conversation_id)
            .where(
                self._expired_condition(policy, evaluated_at),
                ~active_exists,
            )
            .order_by(_conversation_timestamp(), ChatConversation.conversation_id)
            .limit(policy.batch_size)
        )
        return tuple(rows.all())

    async def _has_active_response(self, conversation_id: UUID) -> bool:
        response_id = await self._session.scalar(
            select(ChatResponse.response_id)
            .where(
                ChatResponse.conversation_id == conversation_id,
                ChatResponse.status.in_(_ACTIVE_RESPONSE_STATUSES),
            )
            .limit(1)
        )
        return response_id is not None

    async def delete_conversation(self, conversation_id: UUID) -> ChatDeleteResult:
        conversation = await self._session.get(
            ChatConversation,
            conversation_id,
            with_for_update=True,
        )
        if conversation is None:
            raise ChatMaintenanceConflict(
                status_code=404,
                reason="CHAT_CONVERSATION_NOT_FOUND",
                detail="The requested Chat conversation was not found.",
            )
        if await self._has_active_response(conversation_id):
            raise ChatMaintenanceConflict(
                reason="CHAT_CONVERSATION_ACTIVE",
                detail="A Chat conversation with a created or running response cannot be deleted.",
            )

        trace_ids = tuple(
            (
                await self._session.scalars(
                    select(ChatResponse.trace_id).where(
                        ChatResponse.conversation_id == conversation_id
                    )
                )
            ).all()
        )
        message_count = int(
            await self._session.scalar(
                select(func.count(ChatMessage.message_id)).where(
                    ChatMessage.conversation_id == conversation_id
                )
            )
            or 0
        )
        if trace_ids:
            event_count = int(
                await self._session.scalar(
                    select(func.count(ChatTraceEvent.event_id)).where(
                        ChatTraceEvent.trace_id.in_(trace_ids)
                    )
                )
                or 0
            )
            observation_count = int(
                await self._session.scalar(
                    select(func.count(ChatModelObservation.observation_id)).where(
                        ChatModelObservation.trace_id.in_(trace_ids)
                    )
                )
                or 0
            )
            await self._session.execute(
                delete(ChatModelObservation).where(ChatModelObservation.trace_id.in_(trace_ids))
            )
            await self._session.execute(
                delete(ChatTraceEvent).where(ChatTraceEvent.trace_id.in_(trace_ids))
            )
        else:
            event_count = 0
            observation_count = 0

        response_count = len(trace_ids)
        await self._session.execute(
            delete(ChatResponse).where(ChatResponse.conversation_id == conversation_id)
        )
        await self._session.execute(
            delete(ChatMessage).where(ChatMessage.conversation_id == conversation_id)
        )
        await self._session.execute(
            delete(ChatConversation).where(ChatConversation.conversation_id == conversation_id)
        )
        await self._session.flush()

        return ChatDeleteResult(
            conversation_id=conversation_id,
            content_capture_mode=conversation.content_capture_mode,
            project_id=conversation.project_id,
            deleted_rows={
                "chat_conversations": 1,
                "chat_messages": message_count,
                "chat_responses": response_count,
                "chat_trace_events": event_count,
                "chat_model_observations": observation_count,
            },
        )

    async def purge_expired(
        self,
        policy: ChatRetentionPolicy,
        *,
        evaluated_at: datetime | None = None,
    ) -> ChatRetentionPurgeResult:
        evaluated = evaluated_at or _now()
        candidate_ids = await self._candidate_ids(policy, evaluated_at=evaluated)
        deleted_ids: list[UUID] = []
        skipped_active = 0
        totals = {
            "chat_conversations": 0,
            "chat_messages": 0,
            "chat_responses": 0,
            "chat_trace_events": 0,
            "chat_model_observations": 0,
        }
        for conversation_id in candidate_ids:
            try:
                result = await self.delete_conversation(conversation_id)
            except ChatMaintenanceConflict as exc:
                if exc.reason == "CHAT_CONVERSATION_ACTIVE":
                    skipped_active += 1
                    continue
                if exc.reason == "CHAT_CONVERSATION_NOT_FOUND":
                    continue
                raise
            deleted_ids.append(conversation_id)
            for table_name, count in result.deleted_rows.items():
                totals[table_name] += count

        return ChatRetentionPurgeResult(
            deleted_conversations=len(deleted_ids),
            skipped_active=skipped_active,
            deleted_rows=totals,
            conversation_ids=tuple(deleted_ids),
            evaluated_at=evaluated,
        )
