"""Read-side persistence queries for Chat conversations and response traces."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.infrastructure.database.models import (
    ChatConversation,
    ChatMessage,
    ChatModelObservation,
    ChatResponse,
    ChatTraceEvent,
)


@dataclass(frozen=True, slots=True)
class ConversationListRow:
    conversation: ChatConversation
    message_count: int
    response_count: int
    last_response_status: str | None
    last_chat_model: str | None
    last_embedding_model: str | None


@dataclass(frozen=True, slots=True)
class ConversationDetailRow:
    conversation: ChatConversation
    messages: tuple[ChatMessage, ...]
    responses: tuple[ChatResponse, ...]


@dataclass(frozen=True, slots=True)
class ResponseTraceRow:
    response: ChatResponse
    events: tuple[ChatTraceEvent, ...]
    model_observations: tuple[ChatModelObservation, ...]


class ChatObservabilityStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_conversations(
        self,
        *,
        limit: int,
        offset: int,
        project_id: str | None = None,
        requester_id: str | None = None,
        status: str | None = None,
        chat_model: str | None = None,
        embedding_model: str | None = None,
    ) -> list[ConversationListRow]:
        message_count = (
            select(func.count(ChatMessage.message_id))
            .where(ChatMessage.conversation_id == ChatConversation.conversation_id)
            .correlate(ChatConversation)
            .scalar_subquery()
        )
        response_count = (
            select(func.count(ChatResponse.response_id))
            .where(ChatResponse.conversation_id == ChatConversation.conversation_id)
            .correlate(ChatConversation)
            .scalar_subquery()
        )
        last_response_status = (
            select(ChatResponse.status)
            .where(ChatResponse.conversation_id == ChatConversation.conversation_id)
            .order_by(ChatResponse.started_at.desc(), ChatResponse.response_id.desc())
            .limit(1)
            .correlate(ChatConversation)
            .scalar_subquery()
        )
        last_chat_model = (
            select(ChatResponse.chat_model)
            .where(ChatResponse.conversation_id == ChatConversation.conversation_id)
            .order_by(ChatResponse.started_at.desc(), ChatResponse.response_id.desc())
            .limit(1)
            .correlate(ChatConversation)
            .scalar_subquery()
        )
        last_embedding_model = (
            select(ChatResponse.embedding_model)
            .where(ChatResponse.conversation_id == ChatConversation.conversation_id)
            .order_by(ChatResponse.started_at.desc(), ChatResponse.response_id.desc())
            .limit(1)
            .correlate(ChatConversation)
            .scalar_subquery()
        )
        stmt = select(
            ChatConversation,
            message_count.label("message_count"),
            response_count.label("response_count"),
            last_response_status.label("last_response_status"),
            last_chat_model.label("last_chat_model"),
            last_embedding_model.label("last_embedding_model"),
        )
        if project_id is not None:
            stmt = stmt.where(ChatConversation.project_id == project_id)
        if requester_id is not None:
            stmt = stmt.where(ChatConversation.requester_id == requester_id)
        if status is not None:
            stmt = stmt.where(ChatConversation.status == status)
        if chat_model is not None:
            stmt = stmt.where(
                select(ChatResponse.response_id)
                .where(
                    ChatResponse.conversation_id == ChatConversation.conversation_id,
                    ChatResponse.chat_model == chat_model,
                )
                .exists()
            )
        if embedding_model is not None:
            stmt = stmt.where(
                select(ChatResponse.response_id)
                .where(
                    ChatResponse.conversation_id == ChatConversation.conversation_id,
                    ChatResponse.embedding_model == embedding_model,
                )
                .exists()
            )
        stmt = (
            stmt.order_by(
                func.coalesce(ChatConversation.last_message_at, ChatConversation.created_at).desc(),
                ChatConversation.conversation_id.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            ConversationListRow(
                conversation=row[0],
                message_count=int(row[1] or 0),
                response_count=int(row[2] or 0),
                last_response_status=row[3],
                last_chat_model=row[4],
                last_embedding_model=row[5],
            )
            for row in rows
        ]

    async def get_conversation(self, conversation_id: UUID) -> ConversationDetailRow | None:
        conversation = await self._session.get(ChatConversation, conversation_id)
        if conversation is None:
            return None
        messages = tuple(
            (
                await self._session.scalars(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == conversation_id)
                    .order_by(ChatMessage.sequence, ChatMessage.created_at, ChatMessage.message_id)
                )
            ).all()
        )
        responses = tuple(
            (
                await self._session.scalars(
                    select(ChatResponse)
                    .where(ChatResponse.conversation_id == conversation_id)
                    .order_by(ChatResponse.started_at, ChatResponse.response_id)
                )
            ).all()
        )
        return ConversationDetailRow(
            conversation=conversation,
            messages=messages,
            responses=responses,
        )

    async def get_response_trace(self, response_id: UUID) -> ResponseTraceRow | None:
        response = await self._session.get(ChatResponse, response_id)
        if response is None:
            return None
        events = tuple(
            (
                await self._session.scalars(
                    select(ChatTraceEvent)
                    .where(ChatTraceEvent.trace_id == response.trace_id)
                    .order_by(ChatTraceEvent.sequence, ChatTraceEvent.created_at)
                )
            ).all()
        )
        observations = tuple(
            (
                await self._session.scalars(
                    select(ChatModelObservation)
                    .where(ChatModelObservation.trace_id == response.trace_id)
                    .order_by(ChatModelObservation.observed_at, ChatModelObservation.observation_id)
                )
            ).all()
        )
        return ResponseTraceRow(
            response=response,
            events=events,
            model_observations=observations,
        )
