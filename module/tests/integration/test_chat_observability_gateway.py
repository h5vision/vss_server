from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import httpx2
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.app import create_app
from backend.core.config import Settings
from backend.infrastructure.database.base import Base
from backend.infrastructure.database.models import (
    ChatConversation,
    ChatMessage,
    ChatModelObservation,
    ChatResponse,
    ChatTraceEvent,
)

META = {
    "request_id": "placeholder",
    "project_id": "vss_server-pre-rag",
    "index_id": "vss_server-pre-rag",
    "resolved_by": "exact",
    "model": "qwen3:27b",
    "rag": True,
    "has_evidence": True,
    "top_score": 0.81,
    "threshold": 0.65,
    "reason": "ok",
    "sources": [
        {
            "path": "vss/indexer.py",
            "score": 0.81,
            "start_line": 10,
            "end_line": 24,
            "text": "SOURCE_BODY_MUST_NOT_BE_STORED",
        }
    ],
    "references": [{"path": "vss/indexer.py", "start_line": 10, "end_line": 24}],
    "reference_files": ["vss/indexer.py"],
    "search_profile": {"use_bm25": True, "pool": 20},
    "serving_profile": {
        "embed_model": "bge-m3:latest",
        "embed_dim": 1024,
        "use_bm25": True,
    },
    "bm25_active": True,
    "timing": {
        "embed_ms": 41.2,
        "search_ms": 12.4,
        "bm25_ms": 3.1,
        "pre_llm_ms": 61.0,
    },
}


def _sse(request_id: str, answer: str) -> bytes:
    meta = {**META, "request_id": request_id}
    done = {
        "answer": answer,
        "references": [{"path": "vss/indexer.py"}],
        "reference_files": ["vss/indexer.py"],
        "cited": ["vss/indexer.py"],
        "no_evidence": False,
        "sources": [{"path": "vss/indexer.py", "score": 0.81}],
        "metadata": {
            "request_id": request_id,
            "status": "completed",
            "project_id": "vss_server-pre-rag",
            "index_id": "vss_server-pre-rag",
            "resolved_by": "exact",
            "model": "qwen3:27b",
            "has_evidence": True,
            "reason": "ok",
            "top_score": 0.81,
            "threshold": 0.65,
            "timing": {
                "embed_ms": 41.2,
                "search_ms": 12.4,
                "bm25_ms": 3.1,
                "pre_llm_ms": 61.0,
                "prompt_ms": 0.8,
                "ttft_ms": 612.1,
                "gen_ms": 2810.0,
                "total_ms": 3122.0,
                "decode_tok_s": 50.2,
                "eval_count": 142,
            },
        },
    }
    frames = [
        ("meta", meta),
        ("stage", {"label": "답변 생성 중..."}),
        ("delta", {"text": answer[:4]}),
        ("delta", {"text": answer[4:]}),
        ("done", done),
    ]
    return b"".join(
        f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()
        for event, data in frames
    )


def _database(path: Path) -> tuple[str, object]:
    db_url = f"sqlite+aiosqlite:///{path}"
    engine = create_engine(
        f"sqlite:///{path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    Base.metadata.create_all(engine)
    return db_url, engine


def _settings(
    db_url: str,
    tmp_path: Path,
    *,
    content_mode: str = "question_answer",
) -> Settings:
    return Settings(
        vision_environment="test",
        docs_enabled=False,
        database_url=SecretStr(db_url),
        snapshot_materialization_root=tmp_path / "snapshots",
        snapshot_repository_root=tmp_path / "repos",
        snapshot_recovery_on_startup=False,
        snapshot_chat_observability_enabled=True,
        snapshot_chat_trace_content_mode=content_mode,
        snapshot_chat_delta_batch_bytes=256,
    )


def test_streaming_gateway_relays_exact_sse_and_persists_chat_trace(tmp_path: Path) -> None:
    db_url, engine = _database(tmp_path / "gateway.db")
    received: list[dict] = []
    upstream_bodies: list[bytes] = []

    def vss(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/chat"
        payload = json.loads(request.content)
        received.append(payload)
        request_id = payload["client_request_id"]
        body = _sse(request_id, "변경된 파일만 다시 임베딩합니다.")
        upstream_bodies.append(body)
        return httpx2.Response(
            200,
            content=body,
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    def ollama(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/ps":
            return httpx2.Response(
                200,
                json={
                    "models": [
                        {"name": "qwen3:27b"},
                        {"name": "bge-m3:latest"},
                    ]
                },
            )
        return httpx2.Response(404)

    app = create_app(
        _settings(db_url, tmp_path),
        vss_transport=httpx2.MockTransport(vss),
        ollama_transport=httpx2.MockTransport(ollama),
    )

    with TestClient(app) as client:
        first = client.post(
            "/v1/chat",
            json={
                "project_id": "vss_server-pre-rag",
                "message": "증분 인덱싱은 어떻게 동작해?",
                "stream": True,
                "client_instance_id": "vision-win-01",
                "origin": "vision_frontend",
                "requester_id": "forged-user-must-not-be-trusted",
            },
        )
        assert first.status_code == 200
        assert first.content == upstream_bodies[0]
        conversation_id = UUID(first.headers["X-Chat-Conversation-ID"])
        response_id = UUID(first.headers["X-Chat-Response-ID"])
        trace_id = UUID(first.headers["X-Chat-Trace-ID"])
        assert first.headers["X-VSS-Request-ID"] == str(trace_id)
        assert received[0]["client_request_id"] == str(trace_id)
        assert "conversation_id" not in received[0]
        assert "client_instance_id" not in received[0]
        assert "origin" not in received[0]
        assert "requester_id" not in received[0]

        second = client.post(
            "/v1/chat",
            json={
                "conversation_id": str(conversation_id),
                "project_id": "vss_server-pre-rag",
                "message": "그럼 unchanged chunk는 재사용해?",
                "stream": True,
                "client_instance_id": "vision-win-01",
                "origin": "vision_frontend",
            },
        )
        assert second.status_code == 200
        assert UUID(second.headers["X-Chat-Conversation-ID"]) == conversation_id
        assert received[1]["client_request_id"] == second.headers["X-Chat-Trace-ID"]

        mismatched_client = client.post(
            "/v1/chat",
            json={
                "conversation_id": str(conversation_id),
                "project_id": "vss_server-pre-rag",
                "message": "다른 client가 같은 id를 재사용하면?",
                "stream": True,
                "client_instance_id": "vision-win-02",
                "origin": "vision_frontend",
            },
        )
        assert mismatched_client.status_code == 200
        forked_conversation_id = UUID(mismatched_client.headers["X-Chat-Conversation-ID"])
        assert forked_conversation_id != conversation_id

    with Session(engine) as session:
        conversation = session.get(ChatConversation, conversation_id)
        assert conversation is not None
        assert conversation.requester_type == "client_instance"
        assert conversation.requester_id is None
        assert conversation.client_instance_id == "vision-win-01"
        assert conversation.origin == "vision_frontend"
        assert conversation.content_capture_mode == "question_answer"
        forked = session.get(ChatConversation, forked_conversation_id)
        assert forked is not None
        assert forked.client_instance_id == "vision-win-02"
        assert forked.requester_type == "client_instance"

        messages = list(
            session.scalars(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conversation_id)
                .order_by(ChatMessage.sequence)
            )
        )
        assert [message.sequence for message in messages] == [1, 2, 3, 4]
        assert [message.role for message in messages] == ["user", "assistant", "user", "assistant"]
        assert messages[0].content == "증분 인덱싱은 어떻게 동작해?"
        assert messages[1].content == "변경된 파일만 다시 임베딩합니다."

        response = session.get(ChatResponse, response_id)
        assert response is not None
        assert response.trace_id == trace_id
        assert response.vss_request_id == str(trace_id)
        assert response.status == "completed"
        assert response.outcome == "answered"
        assert response.chat_model == "qwen3:27b"
        assert response.embedding_model == "bge-m3:latest"
        assert response.first_token_at is not None
        assert response.embed_ms == 41.2
        assert response.ttft_ms == 612.1
        assert response.eval_count == 142
        assert response.source_count == 1
        assert response.reference_count == 1

        events = list(
            session.scalars(
                select(ChatTraceEvent)
                .where(ChatTraceEvent.trace_id == trace_id)
                .order_by(ChatTraceEvent.sequence)
            )
        )
        assert events[0].event_type == "request"
        assert {event.event_type for event in events} >= {
            "request",
            "meta",
            "stage",
            "runtime_snapshot",
            "done",
        }
        assert "delta_batch" not in {event.event_type for event in events}
        assert all("forged-user" not in json.dumps(event.payload) for event in events)
        assert all(
            "SOURCE_BODY_MUST_NOT_BE_STORED" not in json.dumps(event.payload) for event in events
        )
        meta_event = next(event for event in events if event.event_type == "meta")
        assert meta_event.payload["sources"] == [
            {"path": "vss/indexer.py", "start_line": 10, "end_line": 24, "score": 0.81}
        ]

        observations = list(
            session.scalars(
                select(ChatModelObservation).where(ChatModelObservation.trace_id == trace_id)
            )
        )
        assert {item.role for item in observations} == {"embedding", "completion"}
        assert all(item.runtime_available for item in observations)
        assert all(item.resident is True for item in observations)
    engine.dispose()


def test_metadata_mode_omits_transcript_content(tmp_path: Path) -> None:
    db_url, engine = _database(tmp_path / "gateway-metadata.db")

    def vss(request: httpx2.Request) -> httpx2.Response:
        request_id = json.loads(request.content)["client_request_id"]
        return httpx2.Response(
            200,
            content=_sse(request_id, "metadata mode answer"),
            headers={"Content-Type": "text/event-stream; charset=utf-8"},
        )

    app = create_app(
        _settings(db_url, tmp_path, content_mode="metadata"),
        vss_transport=httpx2.MockTransport(vss),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat",
            json={"project_id": "vss_server-pre-rag", "message": "secret question", "stream": True},
        )
        assert response.status_code == 200
        conversation_id = UUID(response.headers["X-Chat-Conversation-ID"])
        trace_id = UUID(response.headers["X-Chat-Trace-ID"])

    with Session(engine) as session:
        messages = list(
            session.scalars(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conversation_id)
                .order_by(ChatMessage.sequence)
            )
        )
        assert [message.content for message in messages] == [None, None]
        assert all(message.content_sha256 for message in messages)
        events = list(
            session.scalars(select(ChatTraceEvent).where(ChatTraceEvent.trace_id == trace_id))
        )
        assert "delta_batch" not in {event.event_type for event in events}
        assert all("secret question" not in json.dumps(event.payload) for event in events)
    engine.dispose()


def test_full_debug_mode_batches_stream_deltas(tmp_path: Path) -> None:
    db_url, engine = _database(tmp_path / "gateway-full-debug.db")
    answer = "debug delta payload"

    def vss(request: httpx2.Request) -> httpx2.Response:
        request_id = json.loads(request.content)["client_request_id"]
        return httpx2.Response(
            200,
            content=_sse(request_id, answer),
            headers={"Content-Type": "text/event-stream; charset=utf-8"},
        )

    app = create_app(
        _settings(db_url, tmp_path, content_mode="full_debug"),
        vss_transport=httpx2.MockTransport(vss),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat",
            json={"project_id": "vss_server-pre-rag", "message": "debug question", "stream": True},
        )
        assert response.status_code == 200
        trace_id = UUID(response.headers["X-Chat-Trace-ID"])

    with Session(engine) as session:
        events = list(
            session.scalars(
                select(ChatTraceEvent)
                .where(ChatTraceEvent.trace_id == trace_id)
                .order_by(ChatTraceEvent.sequence)
            )
        )
        delta_batches = [event for event in events if event.event_type == "delta_batch"]
        assert len(delta_batches) == 1
        assert delta_batches[0].payload["text"] == answer
        assert delta_batches[0].payload["bytes"] == len(answer.encode())
    engine.dispose()


def test_gateway_relays_when_trace_database_is_not_configured(tmp_path: Path) -> None:
    upstream: list[bytes] = []

    def vss(request: httpx2.Request) -> httpx2.Response:
        request_id = json.loads(request.content)["client_request_id"]
        body = _sse(request_id, "database independent answer")
        upstream.append(body)
        return httpx2.Response(
            200,
            content=body,
            headers={"Content-Type": "text/event-stream; charset=utf-8"},
        )

    settings = Settings(
        vision_environment="test",
        docs_enabled=False,
        snapshot_materialization_root=tmp_path / "snapshots",
        snapshot_repository_root=tmp_path / "repos",
        snapshot_recovery_on_startup=False,
        snapshot_chat_observability_enabled=True,
        snapshot_chat_trace_content_mode="metadata",
    )
    app = create_app(settings, vss_transport=httpx2.MockTransport(vss))
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat",
            json={"project_id": "vss_server-pre-rag", "message": "hello", "stream": True},
        )
        assert response.status_code == 200
        assert response.content == upstream[0]
        UUID(response.headers["X-Chat-Conversation-ID"])
        UUID(response.headers["X-Chat-Trace-ID"])


def test_gateway_preserves_upstream_non_success_status(tmp_path: Path) -> None:
    db_url, engine = _database(tmp_path / "gateway-error.db")

    def vss(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            503,
            json={"error": {"code": "model_not_loaded", "message": "model unavailable"}},
        )

    app = create_app(
        _settings(db_url, tmp_path),
        vss_transport=httpx2.MockTransport(vss),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat",
            json={"project_id": "vss_server-pre-rag", "message": "hello", "stream": True},
        )
        assert response.status_code == 503
        response_id = UUID(response.headers["X-Chat-Response-ID"])

    with Session(engine) as session:
        saved = session.get(ChatResponse, response_id)
        assert saved is not None
        assert saved.status == "failed"
        assert saved.outcome == "error"
        assert saved.error_code == "HTTP_503"
    engine.dispose()
