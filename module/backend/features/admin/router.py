"""Authenticated Admin REST API Router."""

from __future__ import annotations

import uuid
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import HttpUrl
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from backend.core.errors import ApiError
from backend.features.admin.audit import record_audit
from backend.features.admin.auth import AdminIdentity, require_admin_role
from backend.features.admin.schemas import (
    AdminMutationResponse,
    AdminVssProjectItem,
    AdminVssProjectsResponse,
    AuditLogListResponse,
    AuditLogResponse,
    BranchHeadHistoryItem,
    BranchHeadHistoryListResponse,
    RepositorySyncRunItem,
    RepositorySyncRunListResponse,
    TrackedBranchAdminCreateRequest,
    TrackedBranchAdminListResponse,
    TrackedBranchAdminResponse,
    TrackedBranchAdminUpdateRequest,
)
from backend.features.collection.schemas import (
    BranchCatalogEntry,
    BranchCatalogResponse,
)
from backend.features.collection.service import RepositoryCollectionService
from backend.features.indexing.retry import SnapshotRetryService
from backend.features.repositories.schemas import (
    BranchBindingCreateRequest,
    BranchBindingListResponse,
    BranchBindingResponse,
    BranchBindingUpdateRequest,
    RepositoryCreateRequest,
    RepositoryListResponse,
    RepositoryResponse,
    RepositoryUpdateRequest,
)
from backend.features.repositories.store import (
    BranchBindingStore,
    RepositoryStore,
    StoreLookupError,
)
from backend.features.snapshots.schemas import (
    SnapshotAttemptResponse,
    SnapshotDetailResponse,
    SnapshotListResponse,
    SnapshotRetryResponse,
    SnapshotSummaryResponse,
)
from backend.features.snapshots.store import SnapshotStore
from backend.infrastructure.database.models import (
    AuditLog,
    BranchBinding,
    BranchHeadHistory,
    Repository,
    RepositorySyncRun,
    TrackedBranch,
)
from backend.infrastructure.database.session import get_db_session
from backend.integrations.vss.client import VssHttpClient
from backend.integrations.vss.errors import VssIntegrationError

admin_router = APIRouter(prefix="/admin", tags=["admin"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
RequireViewer = Annotated[AdminIdentity, Depends(require_admin_role("viewer"))]
RequireOperator = Annotated[AdminIdentity, Depends(require_admin_role("operator"))]
RequireAdmin = Annotated[AdminIdentity, Depends(require_admin_role("admin"))]


def _get_request_id(request: Request) -> UUID:
    raw = getattr(request.state, "request_id", None)
    if raw is None:
        return uuid.uuid4()
    if isinstance(raw, UUID):
        return raw
    try:
        return UUID(str(raw))
    except ValueError:
        return uuid.uuid4()


def _to_repo_response(repo: Repository) -> RepositoryResponse:
    return RepositoryResponse(
        repository_id=repo.repository_id,
        canonical_name=repo.canonical_name,
        display_name=repo.display_name,
        provider=repo.provider,
        remote_url=HttpUrl(repo.remote_url),
        default_branch_ref=repo.default_branch_ref,
        active=repo.active,
        created_at=repo.created_at,
        updated_at=repo.updated_at,
    )


def _to_binding_response(binding: BranchBinding) -> BranchBindingResponse:
    return BranchBindingResponse(
        binding_id=binding.binding_id,
        frontend_project_id=binding.frontend_project_id,
        frontend_workspace_name=binding.frontend_workspace_name,
        repository_id=binding.repository_id,
        branch_ref=binding.branch_ref,
        vss_project_id=binding.vss_project_id,
        active=binding.active,
        verified_at=binding.verified_at,
        created_at=binding.created_at,
        updated_at=binding.updated_at,
    )


def _to_tracked_branch_response(branch: TrackedBranch) -> TrackedBranchAdminResponse:
    return TrackedBranchAdminResponse(
        tracked_branch_id=branch.tracked_branch_id,
        repository_id=branch.repository_id,
        branch_ref=branch.branch_ref,
        vss_project_id=branch.vss_project_id,
        tracked=branch.tracked,
        current_head_sha=branch.current_head_sha,
        last_fetched_at=branch.last_fetched_at,
        created_at=branch.created_at,
        updated_at=branch.updated_at,
    )


# -----------------------------------------------------------------------------
# 1. Repositories CRUD & Branches
# -----------------------------------------------------------------------------


@admin_router.get("/repositories", response_model=RepositoryListResponse)
async def list_repositories(
    session: DbSession,
    _identity: RequireViewer,
    active: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> RepositoryListResponse:
    store = RepositoryStore(session)
    repos = await store.list(active=active, limit=limit)
    return RepositoryListResponse(items=[_to_repo_response(r) for r in repos])


@admin_router.post(
    "/repositories",
    response_model=AdminMutationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_repository(
    request: Request,
    payload: RepositoryCreateRequest,
    session: DbSession,
    identity: RequireAdmin,
) -> AdminMutationResponse:
    req_id = _get_request_id(request)
    store = RepositoryStore(session)
    try:
        repo = await store.create(payload)
        await record_audit(
            session,
            request_id=req_id,
            actor=identity.actor_id,
            action="create_repository",
            target_type="repository",
            target_id=str(repo.repository_id),
            outcome="succeeded",
            after_json=_to_repo_response(repo).model_dump(mode="json"),
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ApiError(
            status_code=409,
            reason="REPOSITORY_ALREADY_EXISTS",
            detail="동일한 canonical_name 또는 remote_url을 가진 Repository가 이미 존재합니다.",
            retryable=False,
        ) from exc

    return AdminMutationResponse(
        reason="REPOSITORY_CREATED",
        detail="새로운 Repository가 등록되었습니다.",
        request_id=req_id,
        resource=_to_repo_response(repo).model_dump(mode="json"),
    )


@admin_router.get("/repositories/{repository_id}", response_model=RepositoryResponse)
async def get_repository(
    repository_id: UUID,
    session: DbSession,
    _identity: RequireViewer,
) -> RepositoryResponse:
    store = RepositoryStore(session)
    try:
        repo = await store.get(repository_id)
    except StoreLookupError as exc:
        raise ApiError(
            status_code=404, reason=exc.reason, detail=exc.detail, retryable=False
        ) from exc
    return _to_repo_response(repo)


@admin_router.patch("/repositories/{repository_id}", response_model=AdminMutationResponse)
async def update_repository(
    request: Request,
    repository_id: UUID,
    payload: RepositoryUpdateRequest,
    session: DbSession,
    identity: RequireAdmin,
) -> AdminMutationResponse:
    req_id = _get_request_id(request)
    store = RepositoryStore(session)
    try:
        repo = await store.get(repository_id)
        before_data = _to_repo_response(repo).model_dump(mode="json")
        updated_repo = await store.update(repo, payload)
        after_data = _to_repo_response(updated_repo).model_dump(mode="json")
        await record_audit(
            session,
            request_id=req_id,
            actor=identity.actor_id,
            action="update_repository",
            target_type="repository",
            target_id=str(repository_id),
            before_json=before_data,
            after_json=after_data,
        )
        await session.commit()
    except StoreLookupError as exc:
        raise ApiError(
            status_code=404, reason=exc.reason, detail=exc.detail, retryable=False
        ) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise ApiError(
            status_code=409,
            reason="REPOSITORY_UPDATE_CONFLICT",
            detail="수정하려는 값이 다른 Repository와 충돌합니다.",
            retryable=False,
        ) from exc

    return AdminMutationResponse(
        reason="REPOSITORY_UPDATED",
        detail="Repository 정보가 수정되었습니다.",
        request_id=req_id,
        resource=after_data,
    )


@admin_router.delete("/repositories/{repository_id}", response_model=AdminMutationResponse)
async def delete_repository(
    request: Request,
    repository_id: UUID,
    session: DbSession,
    identity: RequireAdmin,
) -> AdminMutationResponse:
    req_id = _get_request_id(request)
    store = RepositoryStore(session)
    try:
        repo = await store.get(repository_id)
        before_data = _to_repo_response(repo).model_dump(mode="json")
        deactivated = await store.deactivate(repo)
        after_data = _to_repo_response(deactivated).model_dump(mode="json")
        await record_audit(
            session,
            request_id=req_id,
            actor=identity.actor_id,
            action="deactivate_repository",
            target_type="repository",
            target_id=str(repository_id),
            before_json=before_data,
            after_json=after_data,
        )
        await session.commit()
    except StoreLookupError as exc:
        raise ApiError(
            status_code=404, reason=exc.reason, detail=exc.detail, retryable=False
        ) from exc

    return AdminMutationResponse(
        reason="REPOSITORY_DEACTIVATED",
        detail="Repository가 비활성화(soft deactivate)되었습니다.",
        request_id=req_id,
        resource=after_data,
    )


@admin_router.get(
    "/repositories/{repository_id}/branches", response_model=BranchCatalogResponse
)
@admin_router.get(
    "/repositories/{repository_id}/catalog", response_model=BranchCatalogResponse
)
async def get_repository_branches(
    request: Request,
    repository_id: UUID,
    session: DbSession,
    _identity: RequireViewer,
) -> BranchCatalogResponse:
    service: RepositoryCollectionService = request.app.state.collection_service
    heads = await service.catalog(repository_id)

    # Query currently tracked branches for this repository to enrich the catalog response
    tracked_statement = select(TrackedBranch).where(
        TrackedBranch.repository_id == repository_id
    )
    tracked_rows = list(await session.scalars(tracked_statement))
    tracked_map = {tb.branch_ref: tb for tb in tracked_rows}

    branches = [
        BranchCatalogEntry(
            branch_ref=ref,
            head_sha=sha,
            tracked=tracked_map[ref].tracked if ref in tracked_map else False,
            vss_project_id=tracked_map[ref].vss_project_id if ref in tracked_map else None,
        )
        for ref, sha in sorted(heads.items())
    ]
    return BranchCatalogResponse(repository_id=repository_id, branches=branches)


@admin_router.post("/repositories/{repository_id}/sync", response_model=AdminMutationResponse)
async def sync_repository_manual(
    request: Request,
    repository_id: UUID,
    session: DbSession,
    identity: RequireOperator,
) -> AdminMutationResponse:
    req_id = _get_request_id(request)
    service: RepositoryCollectionService = request.app.state.collection_service
    summary = await service.sync_repository(repository_id, trigger="manual")
    summary_dict = {
        "sync_run_id": str(summary.sync_run_id),
        "repository_id": str(summary.repository_id),
        "observed_branches": summary.observed_branches,
        "changed_branches": summary.changed_branches,
        "snapshots_created": summary.snapshots_created,
        "snapshots_accepted": summary.snapshots_accepted,
        "snapshot_failures": summary.snapshot_failures,
        "reason": summary.reason,
        "detail": summary.detail,
    }
    await record_audit(
        session,
        request_id=req_id,
        actor=identity.actor_id,
        action="manual_sync",
        target_type="repository",
        target_id=str(repository_id),
        outcome="succeeded" if summary.snapshot_failures == 0 else "failed",
        details=summary_dict,
    )
    await session.commit()
    detail_msg = (
        f"수동 동기화 완료: 관측 {summary.observed_branches}개, "
        f"변경 {summary.changed_branches}개, "
        f"접수 {summary.snapshots_accepted}개, "
        f"실패 {summary.snapshot_failures}개"
    )
    return AdminMutationResponse(
        reason=(
            "SYNC_COMPLETED"
            if summary.snapshot_failures == 0
            else "SYNC_COMPLETED_WITH_FAILURES"
        ),
        detail=detail_msg,
        request_id=req_id,
        resource=summary_dict,
    )


@admin_router.get(
    "/sync-runs",
    response_model=RepositorySyncRunListResponse,
)
async def list_all_sync_runs(
    session: DbSession,
    _identity: RequireViewer,
    repository_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> RepositorySyncRunListResponse:
    statement = select(RepositorySyncRun).order_by(desc(RepositorySyncRun.started_at))
    if repository_id is not None:
        statement = statement.where(RepositorySyncRun.repository_id == repository_id)
    statement = statement.limit(limit)
    runs = list(await session.scalars(statement))
    items = [
        RepositorySyncRunItem(
            sync_run_id=r.sync_run_id,
            repository_id=r.repository_id,
            trigger=r.trigger,
            state=r.state,
            reason=r.reason,
            detail=r.detail,
            started_at=r.started_at,
            finished_at=r.finished_at,
        )
        for r in runs
    ]
    return RepositorySyncRunListResponse(items=items)


@admin_router.get(
    "/repositories/{repository_id}/sync-runs",
    response_model=RepositorySyncRunListResponse,
)
async def list_repository_sync_runs(
    repository_id: UUID,
    session: DbSession,
    _identity: RequireViewer,
    limit: int = Query(default=50, ge=1, le=200),
) -> RepositorySyncRunListResponse:
    statement = (
        select(RepositorySyncRun)
        .where(RepositorySyncRun.repository_id == repository_id)
        .order_by(desc(RepositorySyncRun.started_at))
        .limit(limit)
    )
    runs = list(await session.scalars(statement))
    items = [
        RepositorySyncRunItem(
            sync_run_id=r.sync_run_id,
            repository_id=r.repository_id,
            trigger=r.trigger,
            state=r.state,
            reason=r.reason,
            detail=r.detail,
            started_at=r.started_at,
            finished_at=r.finished_at,
        )
        for r in runs
    ]
    return RepositorySyncRunListResponse(items=items)


# -----------------------------------------------------------------------------
# 2. Tracked Branches CRUD & History
# -----------------------------------------------------------------------------


@admin_router.get("/tracked-branches", response_model=TrackedBranchAdminListResponse)
async def list_tracked_branches(
    session: DbSession,
    _identity: RequireViewer,
    repository_id: UUID | None = None,
    tracked: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> TrackedBranchAdminListResponse:
    statement = select(TrackedBranch).order_by(
        TrackedBranch.created_at.desc(), TrackedBranch.tracked_branch_id
    )
    if repository_id is not None:
        statement = statement.where(TrackedBranch.repository_id == repository_id)
    if tracked is not None:
        statement = statement.where(TrackedBranch.tracked.is_(tracked))
    statement = statement.limit(limit)
    branches = list(await session.scalars(statement))
    return TrackedBranchAdminListResponse(
        items=[_to_tracked_branch_response(b) for b in branches]
    )


@admin_router.post(
    "/tracked-branches",
    response_model=AdminMutationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_tracked_branch(
    request: Request,
    payload: TrackedBranchAdminCreateRequest,
    session: DbSession,
    identity: RequireAdmin,
) -> AdminMutationResponse:
    req_id = _get_request_id(request)
    repo = await session.get(Repository, payload.repository_id)
    if repo is None:
        raise ApiError(
            status_code=404,
            reason="REPOSITORY_NOT_FOUND",
            detail="지정한 Repository를 찾을 수 없습니다.",
            retryable=False,
        )

    vss_proj_id = payload.vss_project_id
    if not vss_proj_id:
        short_name = payload.branch_ref.removeprefix("refs/heads/").replace("/", "-")
        vss_proj_id = f"{repo.canonical_name}-{short_name}"

    branch = TrackedBranch(
        repository_id=payload.repository_id,
        branch_ref=payload.branch_ref,
        vss_project_id=vss_proj_id,
        tracked=payload.tracked,
    )
    session.add(branch)
    try:
        await session.flush()
        await session.refresh(branch)
        after_data = _to_tracked_branch_response(branch).model_dump(mode="json")
        await record_audit(
            session,
            request_id=req_id,
            actor=identity.actor_id,
            action="create_tracked_branch",
            target_type="tracked_branch",
            target_id=str(branch.tracked_branch_id),
            after_json=after_data,
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ApiError(
            status_code=409,
            reason="TRACKED_BRANCH_ALREADY_EXISTS",
            detail=(
                "해당 Repository와 Branch Ref 또는 VSS Project ID 조합이 이미 등록되어 있습니다."
            ),
            retryable=False,
        ) from exc

    return AdminMutationResponse(
        reason="TRACKED_BRANCH_CREATED",
        detail="추적 대상 Branch가 등록되었습니다.",
        request_id=req_id,
        resource=after_data,
    )


@admin_router.patch(
    "/tracked-branches/{tracked_branch_id}", response_model=AdminMutationResponse
)
async def update_tracked_branch(
    request: Request,
    tracked_branch_id: UUID,
    payload: TrackedBranchAdminUpdateRequest,
    session: DbSession,
    identity: RequireAdmin,
) -> AdminMutationResponse:
    req_id = _get_request_id(request)
    branch = await session.get(TrackedBranch, tracked_branch_id)
    if branch is None:
        raise ApiError(
            status_code=404,
            reason="TRACKED_BRANCH_NOT_FOUND",
            detail="추적 Branch를 찾을 수 없습니다.",
            retryable=False,
        )

    before_data = _to_tracked_branch_response(branch).model_dump(mode="json")
    for field in payload.model_fields_set:
        setattr(branch, field, getattr(payload, field))

    try:
        await session.flush()
        await session.refresh(branch)
        after_data = _to_tracked_branch_response(branch).model_dump(mode="json")
        await record_audit(
            session,
            request_id=req_id,
            actor=identity.actor_id,
            action="update_tracked_branch",
            target_type="tracked_branch",
            target_id=str(tracked_branch_id),
            before_json=before_data,
            after_json=after_data,
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ApiError(
            status_code=409,
            reason="TRACKED_BRANCH_UPDATE_CONFLICT",
            detail="수정하려는 값이 다른 추적 Branch와 충돌합니다.",
            retryable=False,
        ) from exc

    return AdminMutationResponse(
        reason="TRACKED_BRANCH_UPDATED",
        detail="추적 Branch 설정이 변경되었습니다.",
        request_id=req_id,
        resource=after_data,
    )


@admin_router.delete(
    "/tracked-branches/{tracked_branch_id}", response_model=AdminMutationResponse
)
async def delete_tracked_branch(
    request: Request,
    tracked_branch_id: UUID,
    session: DbSession,
    identity: RequireAdmin,
) -> AdminMutationResponse:
    req_id = _get_request_id(request)
    branch = await session.get(TrackedBranch, tracked_branch_id)
    if branch is None:
        raise ApiError(
            status_code=404,
            reason="TRACKED_BRANCH_NOT_FOUND",
            detail="추적 Branch를 찾을 수 없습니다.",
            retryable=False,
        )

    before_data = _to_tracked_branch_response(branch).model_dump(mode="json")
    branch.tracked = False
    await session.flush()
    await session.refresh(branch)
    after_data = _to_tracked_branch_response(branch).model_dump(mode="json")
    await record_audit(
        session,
        request_id=req_id,
        actor=identity.actor_id,
        action="untrack_branch",
        target_type="tracked_branch",
        target_id=str(tracked_branch_id),
        before_json=before_data,
        after_json=after_data,
    )
    await session.commit()
    return AdminMutationResponse(
        reason="TRACKED_BRANCH_DEACTIVATED",
        detail="Branch 추적이 비활성화(untracked)되었습니다.",
        request_id=req_id,
        resource=after_data,
    )


@admin_router.get(
    "/tracked-branches/{tracked_branch_id}/history",
    response_model=BranchHeadHistoryListResponse,
)
async def get_tracked_branch_history(
    tracked_branch_id: UUID,
    session: DbSession,
    _identity: RequireViewer,
    limit: int = Query(default=50, ge=1, le=200),
) -> BranchHeadHistoryListResponse:
    branch = await session.get(TrackedBranch, tracked_branch_id)
    if branch is None:
        raise ApiError(
            status_code=404,
            reason="TRACKED_BRANCH_NOT_FOUND",
            detail="추적 Branch를 찾을 수 없습니다.",
            retryable=False,
        )

    statement = (
        select(BranchHeadHistory)
        .where(BranchHeadHistory.tracked_branch_id == tracked_branch_id)
        .order_by(desc(BranchHeadHistory.observed_at))
        .limit(limit)
    )
    entries = list(await session.scalars(statement))
    items = [
        BranchHeadHistoryItem(
            history_id=e.history_id,
            tracked_branch_id=e.tracked_branch_id,
            previous_head_sha=e.previous_head_sha,
            observed_head_sha=e.observed_head_sha,
            change_type=e.change_type,
            sync_run_id=e.sync_run_id,
            observed_at=e.observed_at,
        )
        for e in entries
    ]
    return BranchHeadHistoryListResponse(items=items)


# -----------------------------------------------------------------------------
# 3. Branch Bindings (Frontend Legacy Compat)
# -----------------------------------------------------------------------------


@admin_router.get("/branch-bindings", response_model=BranchBindingListResponse)
async def list_branch_bindings(
    session: DbSession,
    _identity: RequireViewer,
    frontend_project_id: str | None = None,
    active: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> BranchBindingListResponse:
    store = BranchBindingStore(session)
    bindings = await store.list(
        frontend_project_id=frontend_project_id, active=active, limit=limit
    )
    return BranchBindingListResponse(items=[_to_binding_response(b) for b in bindings])


@admin_router.post(
    "/branch-bindings",
    response_model=AdminMutationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_branch_binding(
    request: Request,
    payload: BranchBindingCreateRequest,
    session: DbSession,
    identity: RequireAdmin,
) -> AdminMutationResponse:
    req_id = _get_request_id(request)
    store = BranchBindingStore(session)
    try:
        binding = await store.create(payload)
        after_data = _to_binding_response(binding).model_dump(mode="json")
        await record_audit(
            session,
            request_id=req_id,
            actor=identity.actor_id,
            action="create_branch_binding",
            target_type="branch_binding",
            target_id=str(binding.binding_id),
            after_json=after_data,
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ApiError(
            status_code=409,
            reason="BRANCH_BINDING_ALREADY_EXISTS",
            detail=(
                "해당 frontend_project_id 또는 workspace_name에 대한 "
                "활성 binding이 이미 존재합니다."
            ),
            retryable=False,
        ) from exc

    return AdminMutationResponse(
        reason="BRANCH_BINDING_CREATED",
        detail="Branch binding이 등록되었습니다.",
        request_id=req_id,
        resource=after_data,
    )


@admin_router.patch(
    "/branch-bindings/{binding_id}", response_model=AdminMutationResponse
)
async def update_branch_binding(
    request: Request,
    binding_id: UUID,
    payload: BranchBindingUpdateRequest,
    session: DbSession,
    identity: RequireAdmin,
) -> AdminMutationResponse:
    req_id = _get_request_id(request)
    store = BranchBindingStore(session)
    try:
        binding = await store.get(binding_id)
        before_data = _to_binding_response(binding).model_dump(mode="json")
        updated = await store.update(binding, payload)
        after_data = _to_binding_response(updated).model_dump(mode="json")
        await record_audit(
            session,
            request_id=req_id,
            actor=identity.actor_id,
            action="update_branch_binding",
            target_type="branch_binding",
            target_id=str(binding_id),
            before_json=before_data,
            after_json=after_data,
        )
        await session.commit()
    except StoreLookupError as exc:
        raise ApiError(
            status_code=404, reason=exc.reason, detail=exc.detail, retryable=False
        ) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise ApiError(
            status_code=409,
            reason="BRANCH_BINDING_UPDATE_CONFLICT",
            detail="수정하려는 binding이 다른 활성 binding과 충돌합니다.",
            retryable=False,
        ) from exc

    return AdminMutationResponse(
        reason="BRANCH_BINDING_UPDATED",
        detail="Branch binding이 수정되었습니다.",
        request_id=req_id,
        resource=after_data,
    )


@admin_router.delete(
    "/branch-bindings/{binding_id}", response_model=AdminMutationResponse
)
async def delete_branch_binding(
    request: Request,
    binding_id: UUID,
    session: DbSession,
    identity: RequireAdmin,
) -> AdminMutationResponse:
    req_id = _get_request_id(request)
    store = BranchBindingStore(session)
    try:
        binding = await store.get(binding_id)
        before_data = _to_binding_response(binding).model_dump(mode="json")
        deactivated = await store.deactivate(binding)
        after_data = _to_binding_response(deactivated).model_dump(mode="json")
        await record_audit(
            session,
            request_id=req_id,
            actor=identity.actor_id,
            action="deactivate_branch_binding",
            target_type="branch_binding",
            target_id=str(binding_id),
            before_json=before_data,
            after_json=after_data,
        )
        await session.commit()
    except StoreLookupError as exc:
        raise ApiError(
            status_code=404, reason=exc.reason, detail=exc.detail, retryable=False
        ) from exc

    return AdminMutationResponse(
        reason="BRANCH_BINDING_DEACTIVATED",
        detail="Branch binding이 비활성화되었습니다.",
        request_id=req_id,
        resource=after_data,
    )


# -----------------------------------------------------------------------------
# 4. Snapshots & Retry
# -----------------------------------------------------------------------------


@admin_router.get("/snapshots", response_model=SnapshotListResponse)
async def list_snapshots(
    session: DbSession,
    _identity: RequireViewer,
    repository_id: UUID | None = None,
    vss_project_id: str | None = None,
    state: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> SnapshotListResponse:
    store = SnapshotStore(session)
    snapshots = await store.list_admin_snapshots(
        repository_id=repository_id,
        vss_project_id=vss_project_id,
        state=state,
        limit=limit,
    )
    items = [
        SnapshotSummaryResponse(
            snapshot_id=s.snapshot_id,
            request_id=s.request_id,
            binding_id=s.binding_id,
            frontend_project_id=s.frontend_project_id,
            repository_id=s.repository_id,
            branch_ref=s.branch_ref,
            vss_project_id=s.vss_project_id,
            base_revision=s.base_revision,
            target_revision=s.target_revision,
            source_type=s.source_type,
            state=s.state,
            attempt_count=s.attempt_count,
            materialized_locator=s.materialized_locator,
            vss_state=s.vss_state,
            vss_reason=s.vss_reason,
            vss_detail=s.vss_detail,
            created_at=s.created_at,
            updated_at=s.updated_at,
        )
        for s in snapshots
    ]
    return SnapshotListResponse(items=items)


@admin_router.get("/snapshots/{snapshot_id}", response_model=SnapshotDetailResponse)
async def get_snapshot_detail(
    snapshot_id: UUID,
    session: DbSession,
    _identity: RequireViewer,
) -> SnapshotDetailResponse:
    store = SnapshotStore(session)
    snapshot = await store.get(snapshot_id)
    if snapshot is None:
        raise ApiError(
            status_code=404,
            reason="SNAPSHOT_NOT_FOUND",
            detail="요청한 Snapshot을 찾을 수 없습니다.",
            retryable=False,
        )

    attempts = await store.get_attempts(snapshot_id)
    delta_counts = await store.count_deltas_by_status(snapshot_id)

    attempt_responses = [
        SnapshotAttemptResponse(
            attempt_id=a.attempt_id,
            snapshot_id=a.snapshot_id,
            request_id=a.request_id,
            attempt_number=a.attempt_number,
            started_at=a.started_at,
            finished_at=a.finished_at,
            upstream_status_code=a.upstream_status_code,
            vss_state=a.vss_state,
            vss_reason=a.vss_reason,
            vss_detail=a.vss_detail,
            retryable=a.retryable,
            latency_ms=a.latency_ms,
            vss_result_json=a.vss_result_json,
        )
        for a in attempts
    ]

    return SnapshotDetailResponse(
        snapshot_id=snapshot.snapshot_id,
        request_id=snapshot.request_id,
        binding_id=snapshot.binding_id,
        frontend_project_id=snapshot.frontend_project_id,
        repository_id=snapshot.repository_id,
        branch_ref=snapshot.branch_ref,
        vss_project_id=snapshot.vss_project_id,
        base_revision=snapshot.base_revision,
        target_revision=snapshot.target_revision,
        source_type=snapshot.source_type,
        state=snapshot.state,
        attempt_count=snapshot.attempt_count,
        materialized_locator=snapshot.materialized_locator,
        vss_state=snapshot.vss_state,
        vss_reason=snapshot.vss_reason,
        vss_detail=snapshot.vss_detail,
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
        attempts=attempt_responses,
        changed_file_count=delta_counts["changed_file_count"],
        deleted_path_count=delta_counts["deleted_path_count"],
        rename_count=delta_counts["rename_count"],
    )


@admin_router.post(
    "/snapshots/{snapshot_id}/retry", response_model=SnapshotRetryResponse
)
async def retry_snapshot(
    request: Request,
    snapshot_id: UUID,
    session: DbSession,
    identity: RequireOperator,
) -> SnapshotRetryResponse:
    req_id = _get_request_id(request)
    retry_service: SnapshotRetryService = getattr(request.app.state, "retry_service", None)
    if retry_service is None:
        raise ApiError(
            status_code=503,
            reason="RETRY_SERVICE_UNAVAILABLE",
            detail="스냅샷 재시도 서비스가 활성화되지 않았습니다.",
            retryable=True,
        )

    outcome = await retry_service.retry(snapshot_id, request_id=req_id)
    await record_audit(
        session,
        request_id=req_id,
        actor=identity.actor_id,
        action="retry_snapshot",
        target_type="snapshot",
        target_id=str(snapshot_id),
        outcome="succeeded" if outcome.status_code == 200 else "failed",
        reason=outcome.body.reason,
        detail=outcome.body.detail,
    )
    await session.commit()
    return outcome.body


# -----------------------------------------------------------------------------
# 5. VSS Projects Catalog & Health
# -----------------------------------------------------------------------------


@admin_router.get("/vss/projects", response_model=AdminVssProjectsResponse)
async def get_vss_projects(
    request: Request,
    _identity: RequireViewer,
) -> AdminVssProjectsResponse:
    vss_client: VssHttpClient = getattr(request.app.state, "vss_client", None)
    if vss_client is None:
        raise ApiError(
            status_code=503,
            reason="VSS_CLIENT_UNAVAILABLE",
            detail="VSS 클라이언트가 설정되지 않았습니다.",
            retryable=True,
        )
    try:
        response = await run_in_threadpool(vss_client.list_projects)
    except VssIntegrationError as exc:
        raise ApiError(
            status_code=503,
            reason=exc.reason,
            detail=exc.detail,
            retryable=exc.retryable,
        ) from exc

    items = [
        AdminVssProjectItem(
            project_id=p.project_id,
            active=True,
            state=p.state.value if p.state else None,
            commit=p.commit,
            chunks=p.chunks,
            indexed_at=p.indexed_at,
        )
        for p in response.projects
    ]
    return AdminVssProjectsResponse(items=items)


# -----------------------------------------------------------------------------
# 6. Audit Logs
# -----------------------------------------------------------------------------


@admin_router.get("/audit-logs", response_model=AuditLogListResponse)
async def list_audit_logs(
    session: DbSession,
    _identity: RequireAdmin,
    target_type: str | None = None,
    target_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> AuditLogListResponse:
    statement = select(AuditLog).order_by(desc(AuditLog.created_at)).limit(limit)
    if target_type is not None:
        statement = statement.where(AuditLog.target_type == target_type)
    if target_id is not None:
        statement = statement.where(AuditLog.target_id == target_id)

    logs = list(await session.scalars(statement))
    items = [
        AuditLogResponse(
            audit_id=log_entry.audit_id,
            request_id=log_entry.request_id,
            actor=log_entry.actor,
            action=log_entry.action,
            target_type=log_entry.target_type,
            target_id=log_entry.target_id,
            outcome=log_entry.outcome,
            reason=log_entry.reason,
            detail=log_entry.detail,
            before_json=log_entry.before_json,
            after_json=log_entry.after_json,
            details=log_entry.details,
            created_at=log_entry.created_at,
        )
        for log_entry in logs
    ]
    return AuditLogListResponse(items=items)
