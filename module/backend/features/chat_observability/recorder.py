"""Fail-open write-side recorder for transparent VSS Chat observability."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.infrastructure.database.models import (
    ChatConversation,
    ChatMessage,
    ChatModelObservation,
    ChatResponse,
    ChatTraceEvent,
)
from backend.integrations.ollama.client import OllamaRuntimeSnapshot

logger = logging.getLogger(__name__)
_ALLOWED_ORIGINS = {"vision_frontend", "admin_debug", "api", "internal_test", "unknown"}
_SENSITIVE_TEXT = re.compile(
    r"(?i)\b(token|password|secret|authorization|api[_-]?key)\s*[:=]\s*([^\s,;]+)"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _content_length(value: str) -> int:
    return len(value.encode("utf-8"))


def _bounded_text(value: object, *, limit: int = 512) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = " ".join(value.split())
    text = _SENSITIVE_TEXT.sub(r"\1=<redacted>", text)
    return text[:limit]


def _float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _safe_profile(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    allowed = {
        "embed_model",
        "embed_dim",
        "chunker_version",
        "use_bm25",
        "pool",
        "rrf_k",
        "top_k",
    }
    return {key: value[key] for key in allowed if key in value}


def _safe_source_metadata(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    allowed = {
        "path",
        "file",
        "source",
        "start_line",
        "end_line",
        "line_start",
        "line_end",
        "score",
        "chunk_id",
        "symbol",
    }
    result: list[dict[str, Any]] = []
    for item in value[:100]:
        if not isinstance(item, dict):
            continue
        safe: dict[str, Any] = {}
        for key in allowed:
            field = item.get(key)
            if isinstance(field, str):
                safe[key] = _bounded_text(field, limit=512)
            elif isinstance(field, (int, float)) and not isinstance(field, bool):
                safe[key] = field
        if safe:
            result.append(safe)
    return result


class ChatTraceRecorder:
    """Persist one Chat response lifecycle without ever blocking the data plane on DB failure."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession] | None,
        conversation_id: UUID,
        response_id: UUID,
        trace_id: UUID,
        content_capture_mode: str,
        delta_batch_bytes: int,
    ) -> None:
        self._sessionmaker = sessionmaker
        self.conversation_id = conversation_id
        self.response_id = response_id
        self.trace_id = trace_id
        self.vss_request_id = str(trace_id)
        self.content_capture_mode = content_capture_mode
        self._delta_batch_bytes = delta_batch_bytes
        self._event_sequence = 1
        self._started_perf = time.perf_counter()
        self._delta_buffer = ""
        self._first_delta_seen = False
        self.chat_model: str | None = None
        self.embedding_model: str | None = None

    @classmethod
    async def start(
        cls,
        *,
        sessionmaker: async_sessionmaker[AsyncSession] | None,
        conversation_id: UUID | None,
        project_id: str | None,
        question: str,
        client_instance_id: str | None,
        origin: str | None,
        content_capture_mode: str,
        delta_batch_bytes: int,
    ) -> ChatTraceRecorder:
        recorder = cls(
            sessionmaker=sessionmaker,
            conversation_id=conversation_id or uuid4(),
            response_id=uuid4(),
            trace_id=uuid4(),
            content_capture_mode=content_capture_mode,
            delta_batch_bytes=delta_batch_bytes,
        )
        await recorder._persist_start(
            project_id=project_id,
            question=question,
            client_instance_id=client_instance_id,
            origin=origin,
        )
        return recorder

    async def _persist_start(
        self,
        *,
        project_id: str | None,
        question: str,
        client_instance_id: str | None,
        origin: str | None,
    ) -> None:
        if self._sessionmaker is None:
            return
        try:
            async with self._sessionmaker() as session:
                conversation = await session.get(
                    ChatConversation,
                    self.conversation_id,
                    with_for_update=True,
                )
                normalized_origin = origin if origin in _ALLOWED_ORIGINS else "unknown"
                requester_type = "client_instance" if client_instance_id else "unknown"
                if (
                    conversation is not None
                    and conversation.client_instance_id
                    and client_instance_id
                    and conversation.client_instance_id != client_instance_id
                ):
                    logger.warning(
                        "chat_conversation_client_mismatch existing=%s requested=%s; forking",
                        conversation.client_instance_id,
                        client_instance_id,
                    )
                    self.conversation_id = uuid4()
                    conversation = None
                if conversation is None:
                    title = None
                    if self.content_capture_mode != "metadata":
                        title = " ".join(question.split())[:255] or None
                    conversation = ChatConversation(
                        conversation_id=self.conversation_id,
                        title=title,
                        requester_type=requester_type,
                        requester_id=None,
                        client_instance_id=(client_instance_id or None),
                        origin=normalized_origin,
                        project_id=project_id,
                        content_capture_mode=self.content_capture_mode,
                        status="active",
                    )
                    session.add(conversation)
                    await session.flush()
                else:
                    self.content_capture_mode = conversation.content_capture_mode
                    if conversation.project_id is None and project_id:
                        conversation.project_id = project_id
                    if conversation.client_instance_id is None and client_instance_id:
                        conversation.client_instance_id = client_instance_id
                        conversation.requester_type = "client_instance"
                    if conversation.origin == "unknown" and normalized_origin != "unknown":
                        conversation.origin = normalized_origin

                maximum = await session.scalar(
                    select(func.max(ChatMessage.sequence)).where(
                        ChatMessage.conversation_id == self.conversation_id
                    )
                )
                sequence = int(maximum or 0) + 1
                captured = question if self.content_capture_mode != "metadata" else None
                message = ChatMessage(
                    conversation_id=self.conversation_id,
                    sequence=sequence,
                    role="user",
                    content=captured,
                    content_sha256=_sha256(question),
                    content_length=_content_length(question),
                    requester_id=conversation.requester_id,
                )
                session.add(message)
                await session.flush()
                session.add(
                    ChatResponse(
                        response_id=self.response_id,
                        trace_id=self.trace_id,
                        conversation_id=self.conversation_id,
                        user_message_id=message.message_id,
                        vss_request_id=self.vss_request_id,
                        project_id=project_id,
                        status="running",
                        started_at=_now(),
                    )
                )
                session.add(
                    ChatTraceEvent(
                        trace_id=self.trace_id,
                        sequence=0,
                        event_type="request",
                        elapsed_ms=0.0,
                        payload={
                            "project_id": project_id,
                            "origin": normalized_origin,
                            "client_instance_id": client_instance_id,
                            "question_sha256": _sha256(question),
                            "question_bytes": _content_length(question),
                        },
                    )
                )
                conversation.last_message_at = _now()
                await session.commit()
        except (SQLAlchemyError, ValueError):
            logger.exception("chat_trace_start_persist_failed trace_id=%s", self.trace_id)

    async def record_meta(self, data: dict[str, Any]) -> None:
        timing = data.get("timing") if isinstance(data.get("timing"), dict) else {}
        serving_profile = _safe_profile(data.get("serving_profile"))
        search_profile = _safe_profile(data.get("search_profile"))
        model = data.get("model") if isinstance(data.get("model"), str) else None
        embed_model = None
        if serving_profile and isinstance(serving_profile.get("embed_model"), str):
            embed_model = serving_profile["embed_model"]
        self.chat_model = model or self.chat_model
        self.embedding_model = embed_model or self.embedding_model
        safe_payload = {
            "request_id": data.get("request_id"),
            "project_id": data.get("project_id"),
            "index_id": data.get("index_id"),
            "resolved_by": data.get("resolved_by"),
            "model": model,
            "rag": data.get("rag"),
            "has_evidence": data.get("has_evidence"),
            "top_score": data.get("top_score"),
            "threshold": data.get("threshold"),
            "reason": data.get("reason"),
            "bm25_active": data.get("bm25_active"),
            "sources": _safe_source_metadata(data.get("sources")),
            "references": _safe_source_metadata(data.get("references")),
            "source_count": len(data.get("sources") or [])
            if isinstance(data.get("sources"), list)
            else 0,
            "reference_count": (
                len(data.get("references") or []) if isinstance(data.get("references"), list) else 0
            ),
            "search_profile": search_profile,
            "serving_profile": serving_profile,
            "timing": {
                key: timing[key]
                for key in timing
                if key
                in {"embed_ms", "search_ms", "bm25_ms", "symbol_ms", "prompt_ms", "pre_llm_ms"}
            },
        }
        await self._update_response_and_event(
            event_type="meta",
            payload=safe_payload,
            updates={
                "project_id": data.get("project_id")
                if isinstance(data.get("project_id"), str)
                else None,
                "index_id": data.get("index_id") if isinstance(data.get("index_id"), str) else None,
                "resolved_by": (
                    data.get("resolved_by") if isinstance(data.get("resolved_by"), str) else None
                ),
                "chat_model": model,
                "embedding_model": embed_model,
                "has_evidence": data.get("has_evidence")
                if isinstance(data.get("has_evidence"), bool)
                else None,
                "top_score": _float(data.get("top_score")),
                "threshold": _float(data.get("threshold")),
                "source_count": safe_payload["source_count"],
                "reference_count": safe_payload["reference_count"],
                "embed_ms": _float(timing.get("embed_ms")),
                "search_ms": _float(timing.get("search_ms")),
                "bm25_ms": _float(timing.get("bm25_ms")),
                "symbol_ms": _float(timing.get("symbol_ms")),
                "prompt_ms": _float(timing.get("prompt_ms")),
                "pre_llm_ms": _float(timing.get("pre_llm_ms")),
            },
        )

    async def record_stage(self, data: dict[str, Any]) -> None:
        label = _bounded_text(data.get("label"), limit=255)
        await self._add_event("stage", {"label": label})

    async def record_delta(self, text: str) -> bool:
        first = not self._first_delta_seen
        if first:
            self._first_delta_seen = True
            await self._set_first_token()
        if self.content_capture_mode == "full_debug" and text:
            self._delta_buffer += text
            if len(self._delta_buffer.encode("utf-8")) >= self._delta_batch_bytes:
                await self.flush_delta()
        return first

    async def flush_delta(self) -> None:
        if not self._delta_buffer:
            return
        text = self._delta_buffer
        self._delta_buffer = ""
        await self._add_event("delta_batch", {"text": text, "bytes": _content_length(text)})

    async def record_done(self, data: dict[str, Any]) -> None:
        await self.flush_delta()
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        timing = metadata.get("timing") if isinstance(metadata.get("timing"), dict) else {}
        answer = data.get("answer") if isinstance(data.get("answer"), str) else ""
        no_evidence = bool(data.get("no_evidence"))
        safe_payload = {
            "no_evidence": no_evidence,
            "sources": _safe_source_metadata(data.get("sources")),
            "references": _safe_source_metadata(data.get("references")),
            "source_count": len(data.get("sources") or [])
            if isinstance(data.get("sources"), list)
            else 0,
            "reference_count": (
                len(data.get("references") or []) if isinstance(data.get("references"), list) else 0
            ),
            "cited_count": len(data.get("cited") or [])
            if isinstance(data.get("cited"), list)
            else 0,
            "metadata": {
                "request_id": metadata.get("request_id"),
                "status": metadata.get("status"),
                "project_id": metadata.get("project_id"),
                "index_id": metadata.get("index_id"),
                "resolved_by": metadata.get("resolved_by"),
                "model": metadata.get("model"),
                "has_evidence": metadata.get("has_evidence"),
                "reason": metadata.get("reason"),
                "top_score": metadata.get("top_score"),
                "threshold": metadata.get("threshold"),
                "timing": timing,
            },
        }
        if self._sessionmaker is None:
            return
        try:
            async with self._sessionmaker() as session:
                response = await session.get(ChatResponse, self.response_id, with_for_update=True)
                conversation = await session.get(
                    ChatConversation,
                    self.conversation_id,
                    with_for_update=True,
                )
                if response is None or conversation is None:
                    return
                maximum = await session.scalar(
                    select(func.max(ChatMessage.sequence)).where(
                        ChatMessage.conversation_id == self.conversation_id
                    )
                )
                captured = answer if self.content_capture_mode != "metadata" else None
                assistant = ChatMessage(
                    conversation_id=self.conversation_id,
                    sequence=int(maximum or 0) + 1,
                    role="assistant",
                    content=captured,
                    content_sha256=_sha256(answer),
                    content_length=_content_length(answer),
                )
                session.add(assistant)
                await session.flush()
                response.assistant_message_id = assistant.message_id
                response.status = "completed"
                response.outcome = "no_evidence" if no_evidence else "answered"
                response.completed_at = _now()
                response.chat_model = (
                    metadata.get("model")
                    if isinstance(metadata.get("model"), str)
                    else self.chat_model
                )
                response.project_id = (
                    metadata.get("project_id")
                    if isinstance(metadata.get("project_id"), str)
                    else response.project_id
                )
                response.index_id = (
                    metadata.get("index_id")
                    if isinstance(metadata.get("index_id"), str)
                    else response.index_id
                )
                response.resolved_by = (
                    metadata.get("resolved_by")
                    if isinstance(metadata.get("resolved_by"), str)
                    else response.resolved_by
                )
                if isinstance(metadata.get("has_evidence"), bool):
                    response.has_evidence = metadata["has_evidence"]
                top_score = _float(metadata.get("top_score"))
                if top_score is not None:
                    response.top_score = top_score
                threshold = _float(metadata.get("threshold"))
                if threshold is not None:
                    response.threshold = threshold
                response.source_count = safe_payload["source_count"]
                response.reference_count = safe_payload["reference_count"]
                for field in (
                    "embed_ms",
                    "search_ms",
                    "bm25_ms",
                    "symbol_ms",
                    "prompt_ms",
                    "pre_llm_ms",
                    "ttft_ms",
                    "gen_ms",
                    "total_ms",
                    "decode_tok_s",
                ):
                    value = _float(timing.get(field))
                    if value is not None:
                        setattr(response, field, value)
                eval_count = _int(timing.get("eval_count"))
                if eval_count is not None:
                    response.eval_count = eval_count
                conversation.last_message_at = _now()
                session.add(
                    ChatTraceEvent(
                        trace_id=self.trace_id,
                        sequence=self._next_event_sequence(),
                        event_type="done",
                        elapsed_ms=self._elapsed_ms(),
                        payload=safe_payload,
                    )
                )
                await session.commit()
        except (SQLAlchemyError, ValueError):
            logger.exception("chat_trace_done_persist_failed trace_id=%s", self.trace_id)

    async def record_error(self, data: dict[str, Any]) -> None:
        await self.flush_delta()
        code = _bounded_text(data.get("code"), limit=128) or "vss_error"
        detail = _bounded_text(data.get("message"), limit=512)
        await self._update_response_and_event(
            event_type="error",
            payload={"code": code, "message": detail},
            updates={
                "status": "failed",
                "outcome": "error",
                "error_code": code,
                "error_detail": detail,
                "completed_at": _now(),
            },
        )

    async def record_upstream_failure(self, status_code: int) -> None:
        await self._update_response_and_event(
            event_type="error",
            payload={"code": "VSS_HTTP_ERROR", "status_code": status_code},
            updates={
                "status": "failed",
                "outcome": "error",
                "error_code": f"HTTP_{status_code}",
                "error_detail": "VSS Chat request returned a non-success HTTP status.",
                "completed_at": _now(),
            },
        )

    async def record_cancelled(self) -> None:
        await self.flush_delta()
        if self._sessionmaker is None:
            return
        try:
            async with self._sessionmaker() as session:
                response = await session.get(ChatResponse, self.response_id, with_for_update=True)
                if response is None or response.status == "completed":
                    return
                response.status = "cancelled"
                response.completed_at = _now()
                await session.commit()
        except SQLAlchemyError:
            logger.exception("chat_trace_cancel_persist_failed trace_id=%s", self.trace_id)

    async def record_runtime_snapshot(
        self,
        snapshot: OllamaRuntimeSnapshot,
        *,
        stage: str,
    ) -> None:
        if self._sessionmaker is None:
            return
        targets = (
            ("embedding", self.embedding_model),
            ("completion", self.chat_model),
        )
        if not any(model for _, model in targets):
            return
        try:
            async with self._sessionmaker() as session:
                for role, model in targets:
                    if not model:
                        continue
                    session.add(
                        ChatModelObservation(
                            trace_id=self.trace_id,
                            role=role,
                            model_name=model,
                            stage=stage[:64],
                            runtime_available=snapshot.available,
                            resident=(model in snapshot.model_names)
                            if snapshot.available
                            else None,
                        )
                    )
                session.add(
                    ChatTraceEvent(
                        trace_id=self.trace_id,
                        sequence=self._next_event_sequence(),
                        event_type="runtime_snapshot",
                        elapsed_ms=self._elapsed_ms(),
                        payload={
                            "stage": stage[:64],
                            "available": snapshot.available,
                            "running_models": list(snapshot.model_names),
                        },
                    )
                )
                await session.commit()
        except (SQLAlchemyError, ValueError):
            logger.exception("chat_runtime_snapshot_persist_failed trace_id=%s", self.trace_id)

    async def _set_first_token(self) -> None:
        if self._sessionmaker is None:
            return
        try:
            async with self._sessionmaker() as session:
                response = await session.get(ChatResponse, self.response_id, with_for_update=True)
                if response is not None and response.first_token_at is None:
                    response.first_token_at = _now()
                    await session.commit()
        except SQLAlchemyError:
            logger.exception("chat_first_token_persist_failed trace_id=%s", self.trace_id)

    async def _add_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._sessionmaker is None:
            return
        try:
            async with self._sessionmaker() as session:
                session.add(
                    ChatTraceEvent(
                        trace_id=self.trace_id,
                        sequence=self._next_event_sequence(),
                        event_type=event_type,
                        elapsed_ms=self._elapsed_ms(),
                        payload=payload,
                    )
                )
                await session.commit()
        except (SQLAlchemyError, ValueError):
            logger.exception(
                "chat_trace_event_persist_failed trace_id=%s event=%s", self.trace_id, event_type
            )

    async def _update_response_and_event(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
        updates: dict[str, Any],
    ) -> None:
        if self._sessionmaker is None:
            return
        try:
            async with self._sessionmaker() as session:
                response = await session.get(ChatResponse, self.response_id, with_for_update=True)
                if response is None:
                    return
                for field, value in updates.items():
                    if value is not None:
                        setattr(response, field, value)
                session.add(
                    ChatTraceEvent(
                        trace_id=self.trace_id,
                        sequence=self._next_event_sequence(),
                        event_type=event_type,
                        elapsed_ms=self._elapsed_ms(),
                        payload=payload,
                    )
                )
                await session.commit()
        except (SQLAlchemyError, ValueError):
            logger.exception(
                "chat_trace_update_persist_failed trace_id=%s event=%s", self.trace_id, event_type
            )

    def _next_event_sequence(self) -> int:
        sequence = self._event_sequence
        self._event_sequence += 1
        return sequence

    def _elapsed_ms(self) -> float:
        return round((time.perf_counter() - self._started_perf) * 1000, 1)
