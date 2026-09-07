"""Admin runtime observability routes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from backend.features.admin.common import Viewer
from backend.features.admin.schemas import AdminRuntimeModelsResponse

router = APIRouter()


@router.get("/runtime/models", response_model=AdminRuntimeModelsResponse)
async def get_runtime_models(
    request: Request,
    _identity: Viewer,
) -> AdminRuntimeModelsResponse:
    runtime = await run_in_threadpool(request.app.state.ollama_runtime_client.running_models)
    return AdminRuntimeModelsResponse(
        available=runtime.available,
        models=list(runtime.model_names),
    )
