"""Admin runtime observability and Ollama lifecycle-control routes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from backend.core.errors import ApiError
from backend.features.admin.audit import record_audit
from backend.features.admin.auth import AdminIdentity
from backend.features.admin.common import Administrator, DbSession, Operator, Viewer
from backend.features.admin.schemas import (
    AdminRuntimeModelAutoUpRequest,
    AdminRuntimeModelAutoUpResponse,
    AdminRuntimeModelControlRequest,
    AdminRuntimeModelDownResponse,
    AdminRuntimeModelReloadResponse,
    AdminRuntimeModelRunRequest,
    AdminRuntimeModelRunResponse,
    AdminRuntimeModelsResponse,
    AdminServiceRestartExecutionStatus,
    AdminServiceRestartRequest,
    AdminServiceRestartResponse,
    AdminServiceRestartStatusResponse,
)
from backend.features.admin.service_restart import (
    ServiceRestartScheduleError,
    ServiceRestartScheduler,
    restart_services,
)
from backend.integrations.ollama.client import OllamaRuntimeError

router = APIRouter()


def _raise_runtime_error(exc: OllamaRuntimeError) -> None:
    raise ApiError(
        status_code=exc.status_code,
        reason=exc.reason,
        detail=exc.detail,
        retryable=exc.retryable,
    ) from exc


async def _runtime_state(client) -> tuple[list[str], list[str]]:
    runtime = await run_in_threadpool(client.running_models)
    auto_up_models = await run_in_threadpool(client.auto_up_model_names)
    return list(runtime.model_names), list(auto_up_models)


async def _audit_runtime_action(
    session: AsyncSession,
    identity: AdminIdentity,
    *,
    action: str,
    model_name: str,
    response,
) -> None:
    await record_audit(
        session,
        request_id=identity.request_id,
        actor=identity.actor_id,
        action=action,
        target_type="ollama_model",
        target_id=model_name,
        after_json=response.model_dump(mode="json"),
    )


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
        auto_up_models=list(runtime.auto_up_model_names),
    )


def _service_restart_scheduler(request: Request) -> ServiceRestartScheduler:
    return ServiceRestartScheduler(request.app.state.settings.snapshot_ops_trigger_dir)


@router.get("/runtime/services", response_model=AdminServiceRestartStatusResponse)
async def get_runtime_services(
    request: Request,
    _identity: Administrator,
) -> AdminServiceRestartStatusResponse:
    scheduler = _service_restart_scheduler(request)
    scopes = ["snapshot_backend", "admin_web", "module_stack"]
    trigger_ready = await run_in_threadpool(scheduler.ready)
    pending_scope = await run_in_threadpool(scheduler.pending_scope) if trigger_ready else None
    in_progress = await run_in_threadpool(scheduler.in_progress) if trigger_ready else False
    execution = await run_in_threadpool(scheduler.execution_status) if trigger_ready else None
    if not trigger_ready:
        controller_state = "not_configured"
    elif in_progress:
        controller_state = "running"
    elif pending_scope is not None:
        controller_state = "scheduled"
    elif execution is not None:
        controller_state = execution.state
    else:
        controller_state = "idle"
    return AdminServiceRestartStatusResponse(
        trigger_ready=trigger_ready,
        controller_state=controller_state,
        pending_scope=pending_scope,
        in_progress=in_progress,
        last_execution=(
            AdminServiceRestartExecutionStatus(
                state=execution.state,
                scope=execution.scope,
                services=list(execution.services),
                request_id=execution.request_id,
                actor=execution.actor,
                scheduled_at=execution.scheduled_at,
                started_at=execution.started_at,
                completed_at=execution.completed_at,
                git_head=execution.git_head,
                detail=execution.detail,
            )
            if execution is not None
            else None
        ),
        scopes=scopes,
        services=["vss-snapshot.service", "vss-admin-web.service"],
    )


@router.post(
    "/runtime/services/restart",
    response_model=AdminServiceRestartResponse,
    status_code=202,
)
async def restart_runtime_services(
    payload: AdminServiceRestartRequest,
    request: Request,
    session: DbSession,
    identity: Administrator,
) -> AdminServiceRestartResponse:
    scheduler = _service_restart_scheduler(request)
    try:
        scheduled = await run_in_threadpool(
            scheduler.schedule,
            payload.scope,
            request_id=identity.request_id,
            actor=identity.actor_id,
        )
    except ServiceRestartScheduleError as exc:
        status_code = 409 if exc.reason in {
            "MODULE_SERVICE_RESTART_IN_PROGRESS",
            "MODULE_SERVICE_RESTART_ALREADY_SCHEDULED",
        } else 503
        raise ApiError(
            status_code=status_code,
            reason=exc.reason,
            detail=exc.detail,
            retryable=exc.retryable,
        ) from exc

    response = AdminServiceRestartResponse(
        reason=(
            "MODULE_SERVICE_RESTART_ALREADY_SCHEDULED"
            if scheduled.already_scheduled
            else "MODULE_SERVICE_RESTART_SCHEDULED"
        ),
        detail=(
            "The requested module service restart was already scheduled."
            if scheduled.already_scheduled
            else "The requested module service restart has been scheduled."
        ),
        request_id=identity.request_id,
        scope=payload.scope,
        services=list(restart_services(payload.scope)),
        already_scheduled=scheduled.already_scheduled,
        reconnect_expected=True,
    )
    try:
        await record_audit(
            session,
            request_id=identity.request_id,
            actor=identity.actor_id,
            action="restart_module_services",
            target_type="module_services",
            target_id=payload.scope,
            after_json=response.model_dump(mode="json"),
        )
        # Persist the audit before the root-owned controller reaches its delayed
        # systemctl calls. This endpoint can restart the Backend that serves it.
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        await run_in_threadpool(scheduler.cancel, scheduled)
        raise ApiError(
            status_code=500,
            reason="MODULE_SERVICE_RESTART_AUDIT_FAILED",
            detail="The restart was cancelled because its audit record could not be persisted.",
            retryable=True,
        ) from exc
    return response


async def _up_runtime_model(
    *,
    model_name: str,
    request: Request,
    session: AsyncSession,
    identity: AdminIdentity,
    audit_action: str,
) -> AdminRuntimeModelRunResponse:
    client = request.app.state.ollama_runtime_client
    try:
        result = await run_in_threadpool(client.load_model, model_name)
    except OllamaRuntimeError as exc:
        _raise_runtime_error(exc)
    models, auto_up_models = await _runtime_state(client)
    response = AdminRuntimeModelRunResponse(
        model=result.model_name,
        already_running=result.already_running,
        models=models,
        auto_up_models=auto_up_models,
    )
    await _audit_runtime_action(
        session,
        identity,
        action=audit_action,
        model_name=result.model_name,
        response=response,
    )
    return response


@router.post("/runtime/models/run", response_model=AdminRuntimeModelRunResponse)
async def run_runtime_model(
    payload: AdminRuntimeModelRunRequest,
    request: Request,
    session: DbSession,
    identity: Operator,
) -> AdminRuntimeModelRunResponse:
    """Backward-compatible alias for the original model preload endpoint."""
    return await _up_runtime_model(
        model_name=payload.model,
        request=request,
        session=session,
        identity=identity,
        audit_action="run_ollama_model",
    )


@router.post("/runtime/models/up", response_model=AdminRuntimeModelRunResponse)
async def up_runtime_model(
    payload: AdminRuntimeModelControlRequest,
    request: Request,
    session: DbSession,
    identity: Operator,
) -> AdminRuntimeModelRunResponse:
    return await _up_runtime_model(
        model_name=payload.model,
        request=request,
        session=session,
        identity=identity,
        audit_action="up_ollama_model",
    )


@router.post("/runtime/models/down", response_model=AdminRuntimeModelDownResponse)
async def down_runtime_model(
    payload: AdminRuntimeModelControlRequest,
    request: Request,
    session: DbSession,
    identity: Operator,
) -> AdminRuntimeModelDownResponse:
    client = request.app.state.ollama_runtime_client
    try:
        result = await run_in_threadpool(client.unload_model, payload.model)
    except OllamaRuntimeError as exc:
        _raise_runtime_error(exc)
    models, auto_up_models = await _runtime_state(client)
    response = AdminRuntimeModelDownResponse(
        model=result.model_name,
        already_stopped=result.already_stopped,
        auto_up_disabled=result.auto_up_disabled,
        models=models,
        auto_up_models=auto_up_models,
    )
    await _audit_runtime_action(
        session,
        identity,
        action="down_ollama_model",
        model_name=result.model_name,
        response=response,
    )
    return response


@router.post("/runtime/models/reload", response_model=AdminRuntimeModelReloadResponse)
async def reload_runtime_model(
    payload: AdminRuntimeModelControlRequest,
    request: Request,
    session: DbSession,
    identity: Operator,
) -> AdminRuntimeModelReloadResponse:
    client = request.app.state.ollama_runtime_client
    try:
        result = await run_in_threadpool(client.reload_model, payload.model)
    except OllamaRuntimeError as exc:
        _raise_runtime_error(exc)
    models, auto_up_models = await _runtime_state(client)
    response = AdminRuntimeModelReloadResponse(
        model=result.model_name,
        was_running=result.was_running,
        models=models,
        auto_up_models=auto_up_models,
    )
    await _audit_runtime_action(
        session,
        identity,
        action="reload_ollama_model",
        model_name=result.model_name,
        response=response,
    )
    return response


@router.put("/runtime/models/auto-up", response_model=AdminRuntimeModelAutoUpResponse)
async def set_runtime_model_auto_up(
    payload: AdminRuntimeModelAutoUpRequest,
    request: Request,
    session: DbSession,
    identity: Operator,
) -> AdminRuntimeModelAutoUpResponse:
    client = request.app.state.ollama_runtime_client
    try:
        result = await run_in_threadpool(client.set_auto_up, payload.model, payload.enabled)
    except OllamaRuntimeError as exc:
        _raise_runtime_error(exc)
    models, auto_up_models = await _runtime_state(client)
    response = AdminRuntimeModelAutoUpResponse(
        model=result.model_name,
        enabled=result.enabled,
        loaded_now=result.loaded_now,
        models=models,
        auto_up_models=auto_up_models,
    )
    await _audit_runtime_action(
        session,
        identity,
        action="set_ollama_model_auto_up",
        model_name=result.model_name,
        response=response,
    )
    return response
