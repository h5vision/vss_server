"""VSS 프로세스가 loopback에서 호출하는 Snapshot source API."""

from __future__ import annotations

import secrets
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request

from backend.core.errors import ApiError
from backend.features.vss_sources.schemas import (
    VssRevisionListResponse,
    VssSourceDescriptorResponse,
)
from backend.features.vss_sources.service import VssSourceService
from backend.features.workspace_overlays.schemas import GitRevision

router = APIRouter(prefix="/internal/vss", tags=["VSS internal integration"])


def _authorize(
    request: Request,
    snapshot_token: str | None,
    authorization: str | None,
) -> None:
    configured = request.app.state.settings.snapshot_vss_api_token
    if configured is None:
        raise ApiError(
            status_code=503,
            reason="VSS_SOURCE_API_NOT_CONFIGURED",
            detail="VSS 내부 조회 API token이 구성되지 않았습니다.",
            retryable=False,
        )
    supplied = snapshot_token
    if supplied is None and authorization is not None:
        scheme, _, credential = authorization.partition(" ")
        if scheme.lower() == "bearer" and credential:
            supplied = credential
    if supplied is None or not secrets.compare_digest(
        supplied,
        configured.get_secret_value(),
    ):
        raise ApiError(
            status_code=401,
            reason="VSS_SOURCE_AUTH_REQUIRED",
            detail="VSS 내부 조회 API 인증에 실패했습니다.",
            retryable=False,
        )


def _service(request: Request) -> VssSourceService:
    sessionmaker = request.app.state.db_sessionmaker
    if sessionmaker is None:
        raise ApiError(
            status_code=503,
            reason="DATABASE_NOT_CONFIGURED",
            detail="Snapshot 데이터베이스가 구성되지 않았습니다.",
            retryable=False,
        )
    return VssSourceService(
        sessionmaker=sessionmaker,
        materializer=request.app.state.snapshot_materializer,
        git_timeout_seconds=request.app.state.settings.snapshot_git_command_timeout_seconds,
    )


@router.get("/source", response_model=VssSourceDescriptorResponse)
async def get_vss_source(
    request: Request,
    project_id: str = Query(min_length=1),
    revision: Annotated[GitRevision | None, Query()] = None,
    x_snapshot_token: str | None = Header(default=None, alias="X-Snapshot-Token"),
    authorization: str | None = Header(default=None),
) -> VssSourceDescriptorResponse:
    _authorize(request, x_snapshot_token, authorization)
    return await _service(request).describe(
        project_id,
        revision=revision,
        request_id=UUID(request.state.request_id),
    )


@router.get("/revisions", response_model=VssRevisionListResponse)
async def get_vss_revisions(
    request: Request,
    project_id: str = Query(min_length=1),
    limit: int = Query(default=100, ge=1, le=500),
    x_snapshot_token: str | None = Header(default=None, alias="X-Snapshot-Token"),
    authorization: str | None = Header(default=None),
) -> VssRevisionListResponse:
    _authorize(request, x_snapshot_token, authorization)
    return await _service(request).revisions(
        project_id,
        limit=limit,
        request_id=UUID(request.state.request_id),
    )
