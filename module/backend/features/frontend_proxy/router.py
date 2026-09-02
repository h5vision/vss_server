"""Read-only routes matching the pinned Vision frontend handlers."""

from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, Query, Request
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from backend.core.errors import ApiError
from backend.features.frontend_proxy.schemas import (
    FrontendBriefingResponse,
    FrontendModelsResponse,
    FrontendProjectsResponse,
)
from backend.features.frontend_proxy.service import (
    to_frontend_briefing,
    to_frontend_models,
    to_frontend_projects,
)
from backend.features.repositories.store import BranchBindingStore, StoreLookupError
from backend.integrations.vss.client import VssHttpClient
from backend.integrations.vss.errors import VssHttpRequestRejected, VssIntegrationError

router = APIRouter(tags=["frontend compatibility"])


def _vss_client(request: Request) -> VssHttpClient:
    return request.app.state.vss_client


def _raise_vss_error(exc: VssIntegrationError) -> NoReturn:
    status_code = 503 if exc.retryable else 502
    raise ApiError(
        status_code=status_code,
        reason=exc.reason,
        detail="VSS 조회 요청을 완료하지 못했습니다.",
        retryable=exc.retryable,
    ) from exc


@router.get("/projects", response_model=FrontendProjectsResponse)
async def get_projects(request: Request) -> FrontendProjectsResponse:
    try:
        response = await run_in_threadpool(_vss_client(request).list_projects)
    except VssIntegrationError as exc:
        _raise_vss_error(exc)
    return to_frontend_projects(response)


@router.get("/models", response_model=FrontendModelsResponse)
async def get_models(request: Request) -> FrontendModelsResponse:
    try:
        response = await run_in_threadpool(_vss_client(request).models)
    except VssIntegrationError as exc:
        _raise_vss_error(exc)
    return to_frontend_models(response)


@router.get("/briefing", response_model=FrontendBriefingResponse)
async def get_briefing(
    request: Request,
    project_id: str = Query(min_length=1),
) -> FrontendBriefingResponse:
    sessionmaker = request.app.state.db_sessionmaker
    if sessionmaker is None:
        raise ApiError(
            status_code=503,
            reason="DATABASE_NOT_CONFIGURED",
            detail="Frontend project binding을 조회할 데이터베이스가 구성되지 않았습니다.",
            retryable=False,
        )

    try:
        async with sessionmaker() as session:
            binding = await BranchBindingStore(session).resolve_active(project_id)
    except StoreLookupError as exc:
        raise ApiError(
            status_code=409,
            reason=exc.reason,
            detail=exc.detail,
            retryable=exc.retryable,
        ) from exc
    except SQLAlchemyError as exc:
        raise ApiError(
            status_code=503,
            reason="DATABASE_UNAVAILABLE",
            detail="Frontend project binding을 조회할 수 없습니다.",
            retryable=True,
        ) from exc

    try:
        response = await run_in_threadpool(
            _vss_client(request).briefing,
            binding.vss_project_id,
        )
    except VssHttpRequestRejected as exc:
        if exc.upstream_status_code == 404:
            raise ApiError(
                status_code=404,
                reason="BRIEFING_NOT_GENERATED",
                detail="해당 프로젝트의 브리핑이 아직 생성되지 않았습니다.",
                retryable=False,
            ) from exc
        _raise_vss_error(exc)
    except VssIntegrationError as exc:
        _raise_vss_error(exc)

    return to_frontend_briefing(
        response,
        frontend_project_id=project_id.strip(),
        vss_project_id=binding.vss_project_id,
    )
