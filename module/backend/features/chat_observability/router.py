"""Transparent VSS Chat relay with fail-open trace persistence."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, Any
from uuid import UUID

import httpx2
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from backend.bootstrap.container import ApplicationContainer, get_container
from backend.core.errors import ApiError
from backend.features.chat_observability.recorder import ChatTraceRecorder
from backend.features.chat_observability.sse import VssSseParser

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat-observability"])
_SAFE_UPSTREAM_HEADERS = {"cache-control", "content-language", "x-accel-buffering"}
ContainerDep = Annotated[ApplicationContainer, Depends(get_container)]


def _parse_conversation_id(value: object) -> UUID | None:
    if value in (None, ""):
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ApiError(
            status_code=422,
            reason="CHAT_CONVERSATION_ID_INVALID",
            detail="conversation_id must be a UUID when provided.",
            retryable=False,
        ) from exc


def _bounded_client_id(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:255]


def _origin(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip().lower() or None


def _chat_question(payload: dict[str, Any]) -> str:
    value = payload.get("message")
    if not isinstance(value, str):
        value = payload.get("query")
    return value if isinstance(value, str) else ""


def _response_headers(upstream: httpx2.Response, recorder: ChatTraceRecorder) -> dict[str, str]:
    headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() in _SAFE_UPSTREAM_HEADERS
    }
    headers.update(
        {
            "X-Chat-Conversation-ID": str(recorder.conversation_id),
            "X-Chat-Response-ID": str(recorder.response_id),
            "X-Chat-Trace-ID": str(recorder.trace_id),
            "X-VSS-Request-ID": recorder.vss_request_id,
        }
    )
    return headers


async def _sample_runtime(
    container: ApplicationContainer, recorder: ChatTraceRecorder, stage: str
) -> None:
    try:
        snapshot = await asyncio.to_thread(container.ollama_runtime_client.running_models)
    except Exception as exc:
        logger.warning(
            "chat_runtime_sample_failed trace_id=%s stage=%s error_type=%s",
            recorder.trace_id,
            stage,
            type(exc).__name__,
        )
        return
    await recorder.record_runtime_snapshot(snapshot, stage=stage)


async def _record_event(
    *,
    recorder: ChatTraceRecorder,
    container: ApplicationContainer,
    event: str,
    data: dict[str, Any],
) -> None:
    if event == "meta":
        await recorder.record_meta(data)
        await _sample_runtime(container, recorder, "retrieval_completed")
        return
    if event == "stage":
        await recorder.record_stage(data)
        return
    if event == "delta":
        text = data.get("text") if isinstance(data.get("text"), str) else ""
        first = await recorder.record_delta(text)
        if first:
            await _sample_runtime(container, recorder, "first_token")
        return
    if event == "done":
        await _sample_runtime(container, recorder, "generation_completed")
        await recorder.record_done(data)
        return
    if event == "error":
        await recorder.record_error(data)


@router.post("/chat")
async def relay_chat(
    request: Request,
    container: ContainerDep,
):
    settings = container.settings
    if not settings.snapshot_chat_observability_enabled:
        raise ApiError(
            status_code=404,
            reason="CHAT_OBSERVABILITY_DISABLED",
            detail="The transparent Chat observability gateway is disabled.",
            retryable=False,
        )

    raw_body = await request.body()
    if len(raw_body) > settings.snapshot_chat_request_max_bytes:
        raise ApiError(
            status_code=413,
            reason="CHAT_REQUEST_TOO_LARGE",
            detail="The Chat request body exceeds the configured observability gateway limit.",
            retryable=False,
        )
    try:
        payload = json.loads(raw_body or b"{}")
    except (TypeError, ValueError) as exc:
        raise ApiError(
            status_code=400,
            reason="CHAT_REQUEST_INVALID_JSON",
            detail="The Chat request body must contain one JSON object.",
            retryable=False,
        ) from exc
    if not isinstance(payload, dict):
        raise ApiError(
            status_code=400,
            reason="CHAT_REQUEST_INVALID_JSON",
            detail="The Chat request body must contain one JSON object.",
            retryable=False,
        )

    conversation_id = _parse_conversation_id(payload.pop("conversation_id", None))
    client_instance_id = _bounded_client_id(payload.pop("client_instance_id", None))
    origin = _origin(payload.pop("origin", None))
    # requester_id is intentionally not accepted from an unauthenticated Chat caller.
    payload.pop("requester_id", None)
    question = _chat_question(payload)
    project_id = payload.get("project_id") if isinstance(payload.get("project_id"), str) else None

    recorder = await ChatTraceRecorder.start(
        sessionmaker=container.db_sessionmaker,
        conversation_id=conversation_id,
        project_id=project_id,
        question=question,
        client_instance_id=client_instance_id,
        origin=origin,
        content_capture_mode=settings.snapshot_chat_trace_content_mode,
        delta_batch_bytes=settings.snapshot_chat_delta_batch_bytes,
    )
    # VSS already treats client_request_id as the canonical per-request identifier.
    # The gateway owns that correlation key so Module and VSS traces are exact 1:1.
    payload["client_request_id"] = recorder.vss_request_id

    try:
        upstream = await container.vss_chat_relay_client.open_chat(payload)
    except httpx2.RequestError:
        await recorder.record_error(
            {"code": "vss_unavailable", "message": "VSS Chat service is unavailable."}
        )
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "vss_unavailable",
                    "message": "VSS Chat service is unavailable.",
                }
            },
            headers={
                "X-Chat-Conversation-ID": str(recorder.conversation_id),
                "X-Chat-Response-ID": str(recorder.response_id),
                "X-Chat-Trace-ID": str(recorder.trace_id),
                "X-VSS-Request-ID": recorder.vss_request_id,
            },
        )

    headers = _response_headers(upstream, recorder)
    content_type = upstream.headers.get("content-type", "application/json")
    is_sse = content_type.lower().startswith("text/event-stream")

    if upstream.status_code < 200 or upstream.status_code >= 300:
        body = await upstream.aread()
        await upstream.aclose()
        await recorder.record_upstream_failure(upstream.status_code)
        return Response(
            content=body,
            status_code=upstream.status_code,
            media_type=content_type.split(";", 1)[0],
            headers=headers,
        )

    if not is_sse:
        body = await upstream.aread()
        await upstream.aclose()
        try:
            result = json.loads(body)
        except (TypeError, ValueError):
            result = None
        if isinstance(result, dict):
            if isinstance(result.get("error"), dict):
                await recorder.record_error(result["error"])
            else:
                await recorder.record_meta(result)
                await _sample_runtime(container, recorder, "generation_completed")
                await recorder.record_done(result)
        return Response(
            content=body,
            status_code=upstream.status_code,
            media_type=content_type.split(";", 1)[0],
            headers=headers,
        )

    parser = VssSseParser()

    async def stream_body():
        try:
            async for chunk in upstream.aiter_bytes():
                for event, data in parser.feed(chunk):
                    await _record_event(
                        recorder=recorder,
                        container=container,
                        event=event,
                        data=data,
                    )
                yield chunk
        except asyncio.CancelledError:
            await recorder.record_cancelled()
            raise
        except httpx2.RequestError as exc:
            logger.warning(
                "chat_relay_stream_failed trace_id=%s error_type=%s",
                recorder.trace_id,
                type(exc).__name__,
            )
            await recorder.record_error(
                {"code": "vss_stream_failed", "message": "VSS Chat stream ended unexpectedly."}
            )
        finally:
            await recorder.flush_delta()
            await upstream.aclose()

    return StreamingResponse(
        stream_body(),
        status_code=upstream.status_code,
        media_type="text/event-stream",
        headers=headers,
    )
