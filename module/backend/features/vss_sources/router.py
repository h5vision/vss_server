"""VSS 프로세스가 loopback에서 호출하는 Snapshot source API."""

from __future__ import annotations

import secrets
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request

from backend.core.errors import ApiError
from backend.features.change_requests.schemas import (
    ChangeRequestProvider,
    ChangeRequestState,
)
from backend.features.repositories.schemas import BranchRef, TagRef
from backend.features.vss_sources.schemas import (
    VssChangeRequestDetailResponse,
    VssChangeRequestListResponse,
    VssContextResponse,
    VssPullCapabilitiesResponse,
    VssReferenceListResponse,
    VssRevisionListResponse,
    VssSourceDescriptorResponse,
)
from backend.features.vss_sources.service import VssSourceService
from backend.features.workspace_overlays.schemas import GitRevision

router = APIRouter(prefix="/internal/vss", tags=["VSS internal integration"])


def _token_setup_hint(request: Request) -> dict[str, str]:
    return {
        "warning": "VSS가 Snapshot 내부 API를 호출하려면 별도 inbound token이 필요합니다.",
        "token_environment_variable": "SNAPSHOT_VSS_API_TOKEN",
        "token_config_path": request.app.state.settings.snapshot_vss_api_token_config_path,
    }


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
            detail=(
                "VSS 내부 조회 API token이 구성되지 않았습니다. "
                "안내된 설정 파일에 token을 구성하십시오."
            ),
            retryable=False,
            extra=_token_setup_hint(request),
        )
    supplied = snapshot_token
    if supplied is None and authorization is not None:
        scheme, _, credential = authorization.partition(" ")
        if scheme.lower() == "bearer" and credential:
            supplied = credential
    if supplied is None:
        raise ApiError(
            status_code=401,
            reason="VSS_SOURCE_AUTH_REQUIRED",
            detail="X-Snapshot-Token 또는 Bearer token이 필요합니다.",
            retryable=False,
            extra=_token_setup_hint(request),
        )
    if not secrets.compare_digest(supplied, configured.get_secret_value()):
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
        index_orchestration_mode=(
            request.app.state.settings.snapshot_index_orchestration_mode
        ),
    )


@router.get("/capabilities", response_model=VssPullCapabilitiesResponse)
async def get_vss_pull_capabilities(
    request: Request,
    x_snapshot_token: str | None = Header(default=None, alias="X-Snapshot-Token"),
    authorization: str | None = Header(default=None),
) -> VssPullCapabilitiesResponse:
    _authorize(request, x_snapshot_token, authorization)
    return _service(request).capabilities(request_id=UUID(request.state.request_id))


@router.get("/refs", response_model=VssReferenceListResponse)
async def get_vss_refs(
    request: Request,
    project_id: str = Query(min_length=1),
    x_snapshot_token: str | None = Header(default=None, alias="X-Snapshot-Token"),
    authorization: str | None = Header(default=None),
) -> VssReferenceListResponse:
    _authorize(request, x_snapshot_token, authorization)
    return await _service(request).refs(
        project_id,
        request_id=UUID(request.state.request_id),
    )


@router.get("/context", response_model=VssContextResponse)
async def get_vss_context(
    request: Request,
    project_id: str = Query(min_length=1),
    revision: Annotated[GitRevision | None, Query()] = None,
    branch_ref: Annotated[BranchRef | None, Query()] = None,
    tag_ref: Annotated[TagRef | None, Query()] = None,
    change_request_provider: ChangeRequestProvider | None = None,
    change_request_number: Annotated[int | None, Query(gt=0)] = None,
    change_request_role: Literal["base", "head", "merge"] | None = None,
    x_snapshot_token: str | None = Header(default=None, alias="X-Snapshot-Token"),
    authorization: str | None = Header(default=None),
) -> VssContextResponse:
    _authorize(request, x_snapshot_token, authorization)
    change_request_values = (
        change_request_provider,
        change_request_number,
        change_request_role,
    )
    has_change_request = any(value is not None for value in change_request_values)
    complete_change_request = all(value is not None for value in change_request_values)
    selector_count = sum(
        value is not None for value in (revision, branch_ref, tag_ref)
    ) + int(has_change_request)
    if selector_count != 1 or (has_change_request and not complete_change_request):
        raise ApiError(
            status_code=422,
            reason="VSS_CONTEXT_SELECTOR_INVALID",
            detail=(
                "revision, branch_ref, tag_ref 또는 완전한 Change Request selector 중 "
                "정확히 하나가 필요합니다."
            ),
            retryable=False,
        )
    return await _service(request).context(
        project_id,
        revision=revision,
        branch_ref=branch_ref,
        tag_ref=tag_ref,
        change_request_provider=change_request_provider,
        change_request_number=change_request_number,
        change_request_role=change_request_role,
        request_id=UUID(request.state.request_id),
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


@router.get("/change-requests", response_model=VssChangeRequestListResponse)
async def get_vss_change_requests(
    request: Request,
    project_id: str = Query(min_length=1),
    state: ChangeRequestState | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    x_snapshot_token: str | None = Header(default=None, alias="X-Snapshot-Token"),
    authorization: str | None = Header(default=None),
) -> VssChangeRequestListResponse:
    _authorize(request, x_snapshot_token, authorization)
    return await _service(request).change_requests(
        project_id,
        state=state,
        limit=limit,
        request_id=UUID(request.state.request_id),
    )


@router.get(
    "/change-requests/{provider}/{external_number}",
    response_model=VssChangeRequestDetailResponse,
)
async def get_vss_change_request(
    request: Request,
    provider: ChangeRequestProvider,
    external_number: int,
    project_id: str = Query(min_length=1),
    x_snapshot_token: str | None = Header(default=None, alias="X-Snapshot-Token"),
    authorization: str | None = Header(default=None),
) -> VssChangeRequestDetailResponse:
    _authorize(request, x_snapshot_token, authorization)
    if external_number <= 0:
        raise ApiError(
            status_code=422,
            reason="REQUEST_VALIDATION_FAILED",
            detail="Change Request number는 양수여야 합니다.",
            retryable=False,
        )
    return await _service(request).change_request(
        project_id,
        provider=provider,
        external_number=external_number,
        request_id=UUID(request.state.request_id),
    )
