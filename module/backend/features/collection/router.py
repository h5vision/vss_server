"""수집 코어의 loopback 전용 내부 API.

`/v1/internal/collection/*`는 `SNAPSHOT_VSS_API_TOKEN`으로 보호되는 내부 경계로
reverse proxy에 공개하지 않는다. IdP/RBAC가 확정되는 Phase 3A-3에서 인증된
`/v1/admin/*` 경계로 대체된다.
"""

from __future__ import annotations

import secrets
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, status

from backend.core.errors import ApiError
from backend.features.collection.schemas import (
    BranchCatalogEntry,
    BranchCatalogResponse,
    BranchHeadHistoryEntry,
    BranchListResponse,
    SyncRunListResponse,
    SyncRunResponse,
    SyncSummaryResponse,
    SyncTriggerRequest,
    TrackedBranchCreateRequest,
    TrackedBranchHistoryResponse,
    TrackedBranchResponse,
)
from backend.features.collection.service import RepositoryCollectionService

router = APIRouter(prefix="/internal/collection", tags=["Collection internal"])


def _authorize(request: Request, snapshot_token: str | None, authorization: str | None) -> None:
    configured = request.app.state.settings.snapshot_vss_api_token
    if configured is None:
        raise ApiError(
            status_code=503,
            reason="COLLECTION_API_NOT_CONFIGURED",
            detail="수집 내부 API token이 구성되지 않았습니다.",
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
            reason="COLLECTION_AUTH_REQUIRED",
            detail="수집 내부 API 인증에 실패했습니다.",
            retryable=False,
        )


def _service(request: Request) -> RepositoryCollectionService:
    service = getattr(request.app.state, "collection_service", None)
    if service is None:
        raise ApiError(
            status_code=503,
            reason="DATABASE_NOT_CONFIGURED",
            detail="Snapshot 데이터베이스가 구성되지 않았습니다.",
            retryable=False,
        )
    return service


def _credentials(
    x_snapshot_token: str | None,
    authorization: str | None,
) -> tuple[str | None, str | None]:
    return x_snapshot_token, authorization


@router.get("/repositories/{repository_id}/catalog", response_model=BranchCatalogResponse)
async def get_branch_catalog(
    request: Request,
    repository_id: UUID,
    x_snapshot_token: str | None = Header(default=None, alias="X-Snapshot-Token"),
    authorization: str | None = Header(default=None),
) -> BranchCatalogResponse:
    _authorize(request, *_credentials(x_snapshot_token, authorization))
    heads = await _service(request).catalog(repository_id)
    return BranchCatalogResponse(
        repository_id=repository_id,
        branches=[
            BranchCatalogEntry(branch_ref=ref, head_sha=sha) for ref, sha in sorted(heads.items())
        ],
    )


@router.get("/repositories/{repository_id}/branches", response_model=BranchListResponse)
async def list_tracked_branches(
    request: Request,
    repository_id: UUID,
    x_snapshot_token: str | None = Header(default=None, alias="X-Snapshot-Token"),
    authorization: str | None = Header(default=None),
) -> BranchListResponse:
    _authorize(request, *_credentials(x_snapshot_token, authorization))
    branches = await _service(request).list_branches(repository_id)
    return BranchListResponse(
        repository_id=repository_id,
        branches=[TrackedBranchResponse.model_validate(branch) for branch in branches],
    )


@router.post(
    "/repositories/{repository_id}/branches",
    response_model=TrackedBranchResponse,
    status_code=status.HTTP_201_CREATED,
)
async def track_branch(
    request: Request,
    repository_id: UUID,
    body: TrackedBranchCreateRequest,
    x_snapshot_token: str | None = Header(default=None, alias="X-Snapshot-Token"),
    authorization: str | None = Header(default=None),
) -> TrackedBranchResponse:
    _authorize(request, *_credentials(x_snapshot_token, authorization))
    branch = await _service(request).track_branch(
        repository_id,
        branch_ref=body.branch_ref,
        vss_project_id=body.vss_project_id,
    )
    return TrackedBranchResponse.model_validate(branch)


@router.delete("/tracked-branches/{tracked_branch_id}", response_model=TrackedBranchResponse)
async def untrack_branch(
    request: Request,
    tracked_branch_id: UUID,
    x_snapshot_token: str | None = Header(default=None, alias="X-Snapshot-Token"),
    authorization: str | None = Header(default=None),
) -> TrackedBranchResponse:
    _authorize(request, *_credentials(x_snapshot_token, authorization))
    branch = await _service(request).untrack_by_id(tracked_branch_id)
    return TrackedBranchResponse.model_validate(branch)


@router.get(
    "/tracked-branches/{tracked_branch_id}/history",
    response_model=TrackedBranchHistoryResponse,
)
async def get_branch_history(
    request: Request,
    tracked_branch_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    x_snapshot_token: str | None = Header(default=None, alias="X-Snapshot-Token"),
    authorization: str | None = Header(default=None),
) -> TrackedBranchHistoryResponse:
    _authorize(request, *_credentials(x_snapshot_token, authorization))
    branch, entries = await _service(request).history(tracked_branch_id, limit=limit)
    return TrackedBranchHistoryResponse(
        tracked_branch_id=branch.tracked_branch_id,
        branch_ref=branch.branch_ref,
        vss_project_id=branch.vss_project_id,
        tracked=branch.tracked,
        current_head_sha=branch.current_head_sha,
        last_fetched_at=branch.last_fetched_at,
        history=[BranchHeadHistoryEntry.model_validate(entry) for entry in entries],
    )


@router.post("/repositories/{repository_id}/sync", response_model=SyncSummaryResponse)
async def trigger_sync(
    request: Request,
    repository_id: UUID,
    body: SyncTriggerRequest,
    x_snapshot_token: str | None = Header(default=None, alias="X-Snapshot-Token"),
    authorization: str | None = Header(default=None),
) -> SyncSummaryResponse:
    _authorize(request, *_credentials(x_snapshot_token, authorization))
    summary = await _service(request).sync_repository(repository_id, trigger=body.trigger)
    return SyncSummaryResponse(
        ok=summary.state == "succeeded",
        sync_run_id=summary.sync_run_id,
        repository_id=summary.repository_id,
        state=summary.state,
        reason=summary.reason,
        detail=summary.detail,
        observed_branches=summary.observed_branches,
        changed_branches=summary.changed_branches,
        snapshots_created=summary.snapshots_created,
        snapshots_accepted=summary.snapshots_accepted,
        snapshot_failures=summary.snapshot_failures,
    )


@router.get("/repositories/{repository_id}/sync-runs", response_model=SyncRunListResponse)
async def list_sync_runs(
    request: Request,
    repository_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    x_snapshot_token: str | None = Header(default=None, alias="X-Snapshot-Token"),
    authorization: str | None = Header(default=None),
) -> SyncRunListResponse:
    _authorize(request, *_credentials(x_snapshot_token, authorization))
    runs = await _service(request).list_sync_runs(repository_id, limit=limit)
    return SyncRunListResponse(
        repository_id=repository_id,
        runs=[SyncRunResponse.model_validate(run) for run in runs],
    )
