"""Admin runtime observability and control routes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from backend.core.errors import ApiError
from backend.features.admin.audit import record_audit
from backend.features.admin.common import DbSession, Operator, Viewer
from backend.features.admin.schemas import (
    AdminRuntimeModelRunRequest,
    AdminRuntimeModelRunResponse,
    AdminRuntimeModelsResponse,
)
from backend.integrations.ollama.client import OllamaRuntimeError

router = APIRouter()


@router.get("/runtime/models", response_model=AdminRuntimeModelsResponse)
async def get_runtime_models(
    request: Request,
    _identity: Viewer,
) -> AdminRuntimeModelsResponse:
    runtime = await run_in_threadpool(request.app.state.ollama_runtime_client.runtime_models)
    return AdminRuntimeModelsResponse(
        available=runtime.available,
        models=list(runtime.model_names),
        installed_models=list(runtime.installed_model_names),
        stopped_models=list(runtime.stopped_model_names),
    )


@router.post("/runtime/models/run", response_model=AdminRuntimeModelRunResponse)
async def run_runtime_model(
    payload: AdminRuntimeModelRunRequest,
    request: Request,
    session: DbSession,
    identity: Operator,
) -> AdminRuntimeModelRunResponse:
    client = request.app.state.ollama_runtime_client
    try:
        result = await run_in_threadpool(client.load_model, payload.model)
    except OllamaRuntimeError as exc:
        raise ApiError(
            status_code=exc.status_code,
            reason=exc.reason,
            detail=exc.detail,
            retryable=exc.retryable,
        ) from exc

    running = await run_in_threadpool(client.running_models)
    response = AdminRuntimeModelRunResponse(
        model=result.model_name,
        already_running=result.already_running,
        models=list(running.model_names),
    )
    await record_audit(
        session,
        request_id=identity.request_id,
        actor=identity.actor_id,
        action="run_ollama_model",
        target_type="ollama_model",
        target_id=result.model_name,
        after_json=response.model_dump(mode="json"),
    )
    return response
