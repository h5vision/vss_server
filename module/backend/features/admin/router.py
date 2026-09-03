"""Authenticated Admin API exposed only through the independent Admin Web BFF."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from backend.core.errors import ApiError
from backend.features.admin.audit import record_audit
from backend.features.admin.auth import AdminIdentity, require_admin_role
from backend.features.admin.pagination import decode_cursor, paginate
from backend.features.admin.schemas import (
    AdminCommitCompareChangeItem,
    AdminCommitCompareResponse,
    AdminCommitDetailResponse,
    AdminCommitListResponse,
    AdminCommitMaterializeRequest,
    AdminCommitMaterializeResponse,
    AdminMutationResponse,
    AdminVssProjectItem,
    AdminVssProjectsResponse,
    AuditLogListResponse,
    AuditLogResponse,
    BranchHeadHistoryItem,
    BranchHeadHistoryListResponse,
    RepositorySyncRunItem,
    RepositorySyncRunListResponse,
    TrackedBranchAdminListResponse,
    TrackedBranchAdminResponse,
    TrackedBranchAdminUpdateRequest,
)
from backend.features.admin.store import AdminStore
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
from backend.features.repository_collection.errors import CollectionError
from backend.features.repository_collection.git_client import RepositoryGitClient
from backend.features.repository_collection.schemas import (
    RepositoryCatalogResult,
    TrackedBranchCreateRequest,
)
from backend.features.repository_collection.store import RepositoryCollectionStore
from backend.features.snapshots.schemas import (
    SnapshotAttemptResponse,
    SnapshotDetailResponse,
    SnapshotListResponse,
    SnapshotRetryResponse,
    SnapshotState,
    SnapshotSummaryResponse,
)
from backend.infrastructure.database.models import BranchBinding, Repository, Snapshot
from backend.infrastructure.database.session import get_db_session
from backend.integrations.vss.errors import VssIntegrationError

router = APIRouter(prefix="/admin", tags=["admin"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
Viewer = Annotated[AdminIdentity, Depends(require_admin_role("viewer"))]
Operator = Annotated[AdminIdentity, Depends(require_admin_role("operator"))]
Administrator = Annotated[AdminIdentity, Depends(require_admin_role("admin"))]

SAFE_VSS_RESULT_FIELDS = {
    "accepted",
    "project_id",
    "state",
    "reason",
    "commit",
    "chunks",
    "indexed_at",
}


def _repository_response(repository: Repository) -> RepositoryResponse:
    return RepositoryResponse.model_validate(
        {
            "repository_id": repository.repository_id,
            "canonical_name": repository.canonical_name,
            "display_name": repository.display_name,
            "provider": repository.provider,
            "remote_url": repository.remote_url,
            "default_branch_ref": repository.default_branch_ref,
            "active": repository.active,
            "created_at": repository.created_at,
            "updated_at": repository.updated_at,
        }
    )


def _binding_response(binding: BranchBinding) -> BranchBindingResponse:
    return BranchBindingResponse.model_validate(binding, from_attributes=True)


def _tracked_response(branch) -> TrackedBranchAdminResponse:
    return TrackedBranchAdminResponse.model_validate(branch, from_attributes=True)


def _safe_locator(snapshot: Snapshot) -> str | None:
    if snapshot.materialized_locator is None:
        return None
    return f"revision:{snapshot.target_revision}"


def _snapshot_summary(snapshot: Snapshot) -> SnapshotSummaryResponse:
    return SnapshotSummaryResponse(
        snapshot_id=snapshot.snapshot_id,
        request_id=snapshot.request_id,
        binding_id=snapshot.binding_id,
        tracked_branch_id=snapshot.tracked_branch_id,
        frontend_project_id=snapshot.frontend_project_id,
        repository_id=snapshot.repository_id,
        branch_ref=snapshot.branch_ref,
        vss_project_id=snapshot.vss_project_id,
        base_revision=snapshot.base_revision,
        target_revision=snapshot.target_revision,
        source_type=snapshot.source_type,
        state=snapshot.state,
        attempt_count=snapshot.attempt_count,
        materialized_locator=_safe_locator(snapshot),
        vss_state=snapshot.vss_state,
        vss_reason=snapshot.vss_reason,
        vss_detail=snapshot.vss_detail,
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
    )


def _safe_vss_result(value: dict | None) -> dict | None:
    if value is None:
        return None
    return {key: value[key] for key in SAFE_VSS_RESULT_FIELDS if key in value}


def _snapshot_detail(snapshot: Snapshot) -> SnapshotDetailResponse:
    summary = _snapshot_summary(snapshot).model_dump()
    attempts = [
        SnapshotAttemptResponse(
            attempt_id=attempt.attempt_id,
            snapshot_id=attempt.snapshot_id,
            request_id=attempt.request_id,
            attempt_number=attempt.attempt_number,
            started_at=attempt.started_at,
            finished_at=attempt.finished_at,
            upstream_status_code=attempt.upstream_status_code,
            vss_state=attempt.vss_state,
            vss_reason=attempt.vss_reason,
            vss_detail=attempt.vss_detail,
            retryable=attempt.retryable,
            latency_ms=attempt.latency_ms,
            vss_result_json=_safe_vss_result(attempt.vss_result_json),
        )
        for attempt in snapshot.attempts
    ]
    return SnapshotDetailResponse(
        **summary,
        attempts=attempts,
        changed_file_count=sum(
            item.status in {"added", "modified", "renamed"} for item in snapshot.deltas
        ),
        deleted_path_count=sum(item.status == "deleted" for item in snapshot.deltas),
        rename_count=sum(item.status == "renamed" for item in snapshot.deltas),
    )


def _not_found(error: StoreLookupError) -> ApiError:
    return ApiError(
        status_code=404,
        reason=error.reason,
        detail=error.detail,
        retryable=error.retryable,
    )


def _collection_error(error: CollectionError) -> ApiError:
    return ApiError(
        status_code=error.status_code,
        reason=error.reason,
        detail=error.detail,
        retryable=error.retryable,
    )


def _collection_service(request: Request):
    service = getattr(request.app.state, "repository_collection_service", None)
    if service is None:
        raise ApiError(
            status_code=503,
            reason="ADMIN_DATABASE_UNAVAILABLE",
            detail="Repository collection is unavailable because the database is not configured.",
            retryable=True,
        )
    return service


@router.get("/repositories", response_model=RepositoryListResponse)
async def list_repositories(
    session: DbSession,
    _identity: Viewer,
    active: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = None,
) -> RepositoryListResponse:
    offset = decode_cursor(cursor)
    rows = await RepositoryStore(session).list(active=active, limit=limit + 1, offset=offset)
    items, next_cursor = paginate(rows, limit=limit, offset=offset)
    return RepositoryListResponse(
        items=[_repository_response(item) for item in items],
        next_cursor=next_cursor,
    )


@router.get("/repositories/{repository_id}", response_model=RepositoryResponse)
async def get_repository(repository_id: UUID, session: DbSession, _identity: Viewer):
    try:
        return _repository_response(await RepositoryStore(session).get(repository_id))
    except StoreLookupError as exc:
        raise _not_found(exc) from exc


@router.post("/repositories", response_model=AdminMutationResponse, status_code=201)
async def create_repository(
    payload: RepositoryCreateRequest,
    session: DbSession,
    identity: Administrator,
) -> AdminMutationResponse:
    try:
        repository = await RepositoryStore(session).create(payload)
    except IntegrityError as exc:
        raise ApiError(
            status_code=409,
            reason="REPOSITORY_ALREADY_EXISTS",
            detail="A Repository with the same canonical name or remote URL already exists.",
            retryable=False,
        ) from exc
    resource = _repository_response(repository).model_dump(mode="json")
    await record_audit(
        session,
        request_id=identity.request_id,
        actor=identity.actor_id,
        action="create_repository",
        target_type="repository",
        target_id=str(repository.repository_id),
        after_json=resource,
    )
    return AdminMutationResponse(
        reason="REPOSITORY_CREATED",
        detail="Repository was registered.",
        request_id=identity.request_id,
        resource=resource,
    )


@router.patch("/repositories/{repository_id}", response_model=AdminMutationResponse)
async def update_repository(
    repository_id: UUID,
    payload: RepositoryUpdateRequest,
    session: DbSession,
    identity: Administrator,
) -> AdminMutationResponse:
    store = RepositoryStore(session)
    try:
        repository = await store.get(repository_id)
        before = _repository_response(repository).model_dump(mode="json")
        repository = await store.update(repository, payload)
    except StoreLookupError as exc:
        raise _not_found(exc) from exc
    except IntegrityError as exc:
        raise ApiError(
            status_code=409,
            reason="REPOSITORY_UPDATE_CONFLICT",
            detail="The requested Repository values conflict with an existing row.",
            retryable=False,
        ) from exc
    resource = _repository_response(repository).model_dump(mode="json")
    await record_audit(
        session,
        request_id=identity.request_id,
        actor=identity.actor_id,
        action="update_repository",
        target_type="repository",
        target_id=str(repository_id),
        before_json=before,
        after_json=resource,
    )
    return AdminMutationResponse(
        reason="REPOSITORY_UPDATED",
        detail="Repository was updated.",
        request_id=identity.request_id,
        resource=resource,
    )


@router.delete("/repositories/{repository_id}", response_model=AdminMutationResponse)
async def deactivate_repository(
    repository_id: UUID,
    session: DbSession,
    identity: Administrator,
) -> AdminMutationResponse:
    store = RepositoryStore(session)
    try:
        repository = await store.get(repository_id)
        before = _repository_response(repository).model_dump(mode="json")
        repository = await store.deactivate(repository)
    except StoreLookupError as exc:
        raise _not_found(exc) from exc
    resource = _repository_response(repository).model_dump(mode="json")
    await record_audit(
        session,
        request_id=identity.request_id,
        actor=identity.actor_id,
        action="deactivate_repository",
        target_type="repository",
        target_id=str(repository_id),
        before_json=before,
        after_json=resource,
    )
    return AdminMutationResponse(
        reason="REPOSITORY_DEACTIVATED",
        detail="Repository was deactivated without deleting history.",
        request_id=identity.request_id,
        resource=resource,
    )


@router.get(
    "/repositories/{repository_id}/branches",
    response_model=RepositoryCatalogResult,
)
async def catalog_repository(repository_id: UUID, request: Request, _identity: Viewer):
    try:
        return await _collection_service(request).catalog_repository(repository_id)
    except CollectionError as exc:
        raise _collection_error(exc) from exc


@router.post("/repositories/{repository_id}/sync", response_model=AdminMutationResponse)
async def sync_repository(
    repository_id: UUID,
    request: Request,
    session: DbSession,
    identity: Operator,
):
    try:
        result = await _collection_service(request).sync_repository(
            repository_id,
            trigger="manual",
            request_id=identity.request_id,
        )
    except CollectionError as exc:
        raise _collection_error(exc) from exc
    if not result.ok:
        raise ApiError(
            status_code=503 if result.retryable else 502,
            reason=result.reason,
            detail=result.detail,
            retryable=result.retryable,
            extra={"resource": result.model_dump(mode="json")},
        )
    resource = result.model_dump(mode="json")
    await record_audit(
        session,
        request_id=identity.request_id,
        actor=identity.actor_id,
        action="sync_repository",
        target_type="repository",
        target_id=str(repository_id),
        after_json=resource,
    )
    return AdminMutationResponse(
        reason=result.reason,
        detail=result.detail,
        request_id=identity.request_id,
        resource=resource,
    )


@router.get("/repository-sync-runs", response_model=RepositorySyncRunListResponse)
async def list_sync_runs(
    session: DbSession,
    _identity: Viewer,
    repository_id: UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = None,
) -> RepositorySyncRunListResponse:
    offset = decode_cursor(cursor)
    rows = await AdminStore(session).list_sync_runs(
        repository_id=repository_id,
        limit=limit + 1,
        offset=offset,
    )
    runs, next_cursor = paginate(rows, limit=limit, offset=offset)
    return RepositorySyncRunListResponse(
        items=[RepositorySyncRunItem.model_validate(item) for item in runs],
        next_cursor=next_cursor,
    )


@router.get("/tracked-branches", response_model=TrackedBranchAdminListResponse)
async def list_tracked_branches(
    session: DbSession,
    _identity: Viewer,
    repository_id: UUID | None = None,
    tracked: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = None,
) -> TrackedBranchAdminListResponse:
    offset = decode_cursor(cursor)
    rows = await AdminStore(session).list_tracked_branches(
        repository_id=repository_id,
        tracked=tracked,
        limit=limit + 1,
        offset=offset,
    )
    branches, next_cursor = paginate(rows, limit=limit, offset=offset)
    return TrackedBranchAdminListResponse(
        items=[_tracked_response(item) for item in branches],
        next_cursor=next_cursor,
    )


@router.post("/tracked-branches", response_model=AdminMutationResponse, status_code=201)
async def register_tracked_branch(
    payload: TrackedBranchCreateRequest,
    request: Request,
    session: DbSession,
    identity: Administrator,
) -> AdminMutationResponse:
    try:
        branch = await _collection_service(request).register_tracked_branch(payload)
    except CollectionError as exc:
        raise _collection_error(exc) from exc
    resource = branch.model_dump(mode="json")
    await record_audit(
        session,
        request_id=identity.request_id,
        actor=identity.actor_id,
        action="register_tracked_branch",
        target_type="tracked_branch",
        target_id=str(branch.tracked_branch_id),
        after_json=resource,
    )
    return AdminMutationResponse(
        reason="TRACKED_BRANCH_REGISTERED",
        detail="The selected exact Branch is now tracked.",
        request_id=identity.request_id,
        resource=resource,
    )


@router.patch("/tracked-branches/{tracked_branch_id}", response_model=AdminMutationResponse)
async def update_tracked_branch(
    tracked_branch_id: UUID,
    payload: TrackedBranchAdminUpdateRequest,
    session: DbSession,
    identity: Administrator,
) -> AdminMutationResponse:
    store = RepositoryCollectionStore(session)
    try:
        branch = await store.get_tracked_branch(tracked_branch_id)
        before = _tracked_response(branch).model_dump(mode="json")
        for field in payload.model_fields_set:
            setattr(branch, field, getattr(payload, field))
        await session.flush()
        await session.refresh(branch)
    except CollectionError as exc:
        raise _collection_error(exc) from exc
    except IntegrityError as exc:
        raise ApiError(
            status_code=409,
            reason="TRACKED_BRANCH_CONFLICT",
            detail="The VSS project ID conflicts with another tracked Branch.",
            retryable=False,
        ) from exc
    resource = _tracked_response(branch).model_dump(mode="json")
    await record_audit(
        session,
        request_id=identity.request_id,
        actor=identity.actor_id,
        action="update_tracked_branch",
        target_type="tracked_branch",
        target_id=str(tracked_branch_id),
        before_json=before,
        after_json=resource,
    )
    return AdminMutationResponse(
        reason="TRACKED_BRANCH_UPDATED",
        detail="Tracked Branch settings were updated.",
        request_id=identity.request_id,
        resource=resource,
    )


@router.delete("/tracked-branches/{tracked_branch_id}", response_model=AdminMutationResponse)
async def deactivate_tracked_branch(
    tracked_branch_id: UUID,
    session: DbSession,
    identity: Administrator,
) -> AdminMutationResponse:
    store = RepositoryCollectionStore(session)
    try:
        branch = await store.get_tracked_branch(tracked_branch_id)
    except CollectionError as exc:
        raise _collection_error(exc) from exc
    before = _tracked_response(branch).model_dump(mode="json")
    branch.tracked = False
    await session.flush()
    await session.refresh(branch)
    resource = _tracked_response(branch).model_dump(mode="json")
    await record_audit(
        session,
        request_id=identity.request_id,
        actor=identity.actor_id,
        action="deactivate_tracked_branch",
        target_type="tracked_branch",
        target_id=str(tracked_branch_id),
        before_json=before,
        after_json=resource,
    )
    return AdminMutationResponse(
        reason="TRACKED_BRANCH_DEACTIVATED",
        detail="Tracked Branch collection was deactivated without deleting history.",
        request_id=identity.request_id,
        resource=resource,
    )


@router.get(
    "/tracked-branches/{tracked_branch_id}/head-history",
    response_model=BranchHeadHistoryListResponse,
)
async def list_head_history(
    tracked_branch_id: UUID,
    session: DbSession,
    _identity: Viewer,
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = None,
) -> BranchHeadHistoryListResponse:
    try:
        await RepositoryCollectionStore(session).get_tracked_branch(tracked_branch_id)
    except CollectionError as exc:
        raise _collection_error(exc) from exc
    offset = decode_cursor(cursor)
    rows = await AdminStore(session).list_head_history(
        tracked_branch_id,
        limit=limit + 1,
        offset=offset,
    )
    history, next_cursor = paginate(rows, limit=limit, offset=offset)
    return BranchHeadHistoryListResponse(
        items=[BranchHeadHistoryItem.model_validate(item) for item in history],
        next_cursor=next_cursor,
    )


@router.get("/branch-bindings", response_model=BranchBindingListResponse)
async def list_branch_bindings(
    session: DbSession,
    _identity: Viewer,
    frontend_project_id: str | None = None,
    active: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = None,
) -> BranchBindingListResponse:
    offset = decode_cursor(cursor)
    rows = await BranchBindingStore(session).list(
        frontend_project_id=frontend_project_id,
        active=active,
        limit=limit + 1,
        offset=offset,
    )
    bindings, next_cursor = paginate(rows, limit=limit, offset=offset)
    return BranchBindingListResponse(
        items=[_binding_response(item) for item in bindings],
        next_cursor=next_cursor,
    )


@router.post("/branch-bindings", response_model=AdminMutationResponse, status_code=201)
async def create_branch_binding(
    payload: BranchBindingCreateRequest,
    session: DbSession,
    identity: Administrator,
) -> AdminMutationResponse:
    try:
        binding = await BranchBindingStore(session).create(payload)
    except IntegrityError as exc:
        raise ApiError(
            status_code=409,
            reason="BRANCH_BINDING_CONFLICT",
            detail="The active Frontend or VSS binding conflicts with an existing row.",
            retryable=False,
        ) from exc
    resource = _binding_response(binding).model_dump(mode="json")
    await record_audit(
        session,
        request_id=identity.request_id,
        actor=identity.actor_id,
        action="create_branch_binding",
        target_type="branch_binding",
        target_id=str(binding.binding_id),
        after_json=resource,
    )
    return AdminMutationResponse(
        reason="BRANCH_BINDING_CREATED",
        detail="Frontend binding was created.",
        request_id=identity.request_id,
        resource=resource,
    )


@router.patch("/branch-bindings/{binding_id}", response_model=AdminMutationResponse)
async def update_branch_binding(
    binding_id: UUID,
    payload: BranchBindingUpdateRequest,
    session: DbSession,
    identity: Administrator,
) -> AdminMutationResponse:
    store = BranchBindingStore(session)
    try:
        binding = await store.get(binding_id)
        before = _binding_response(binding).model_dump(mode="json")
        binding = await store.update(binding, payload)
    except StoreLookupError as exc:
        raise _not_found(exc) from exc
    except IntegrityError as exc:
        raise ApiError(
            status_code=409,
            reason="BRANCH_BINDING_CONFLICT",
            detail="The active Frontend or VSS binding conflicts with an existing row.",
            retryable=False,
        ) from exc
    resource = _binding_response(binding).model_dump(mode="json")
    await record_audit(
        session,
        request_id=identity.request_id,
        actor=identity.actor_id,
        action="update_branch_binding",
        target_type="branch_binding",
        target_id=str(binding_id),
        before_json=before,
        after_json=resource,
    )
    return AdminMutationResponse(
        reason="BRANCH_BINDING_UPDATED",
        detail="Frontend binding was updated.",
        request_id=identity.request_id,
        resource=resource,
    )


@router.delete("/branch-bindings/{binding_id}", response_model=AdminMutationResponse)
async def deactivate_branch_binding(
    binding_id: UUID,
    session: DbSession,
    identity: Administrator,
) -> AdminMutationResponse:
    store = BranchBindingStore(session)
    try:
        binding = await store.get(binding_id)
        before = _binding_response(binding).model_dump(mode="json")
        binding = await store.deactivate(binding)
    except StoreLookupError as exc:
        raise _not_found(exc) from exc
    resource = _binding_response(binding).model_dump(mode="json")
    await record_audit(
        session,
        request_id=identity.request_id,
        actor=identity.actor_id,
        action="deactivate_branch_binding",
        target_type="branch_binding",
        target_id=str(binding_id),
        before_json=before,
        after_json=resource,
    )
    return AdminMutationResponse(
        reason="BRANCH_BINDING_DEACTIVATED",
        detail="Frontend binding was deactivated without deleting history.",
        request_id=identity.request_id,
        resource=resource,
    )


@router.get("/snapshots", response_model=SnapshotListResponse)
async def list_snapshots(
    session: DbSession,
    _identity: Viewer,
    repository_id: UUID | None = None,
    tracked_branch_id: UUID | None = None,
    state: SnapshotState | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = None,
) -> SnapshotListResponse:
    offset = decode_cursor(cursor)
    rows = await AdminStore(session).list_snapshots(
        repository_id=repository_id,
        tracked_branch_id=tracked_branch_id,
        state=state,
        limit=limit + 1,
        offset=offset,
    )
    snapshots, next_cursor = paginate(rows, limit=limit, offset=offset)
    return SnapshotListResponse(
        items=[_snapshot_summary(item) for item in snapshots],
        next_cursor=next_cursor,
    )


@router.get("/snapshots/{snapshot_id}", response_model=SnapshotDetailResponse)
async def get_snapshot(snapshot_id: UUID, session: DbSession, _identity: Viewer):
    snapshot = await AdminStore(session).get_snapshot_detail(snapshot_id)
    if snapshot is None:
        raise ApiError(
            status_code=404,
            reason="SNAPSHOT_NOT_FOUND",
            detail="The requested Snapshot was not found.",
            retryable=False,
        )
    return _snapshot_detail(snapshot)


@router.post("/snapshots/{snapshot_id}/retry", response_model=SnapshotRetryResponse)
async def retry_snapshot(
    snapshot_id: UUID,
    request: Request,
    response: Response,
    session: DbSession,
    identity: Operator,
) -> SnapshotRetryResponse:
    service = getattr(request.app.state, "snapshot_retry_service", None)
    if service is None:
        raise ApiError(
            status_code=503,
            reason="ADMIN_DATABASE_UNAVAILABLE",
            detail="Snapshot retry is unavailable because the database is not configured.",
            retryable=True,
        )
    outcome = await service.retry(snapshot_id, request_id=identity.request_id)
    response.status_code = outcome.status_code
    await record_audit(
        session,
        request_id=identity.request_id,
        actor=identity.actor_id,
        action="retry_snapshot",
        target_type="snapshot",
        target_id=str(snapshot_id),
        after_json=outcome.body.model_dump(mode="json"),
    )
    return outcome.body


@router.get("/vss/projects", response_model=AdminVssProjectsResponse)
async def list_vss_projects(request: Request, _identity: Viewer) -> AdminVssProjectsResponse:
    try:
        response = await run_in_threadpool(request.app.state.vss_client.list_projects)
    except VssIntegrationError as exc:
        raise ApiError(
            status_code=503 if exc.retryable else 502,
            reason=exc.reason,
            detail="VSS project catalog is unavailable.",
            retryable=exc.retryable,
        ) from exc
    return AdminVssProjectsResponse(
        items=[
            AdminVssProjectItem(
                project_id=item.project_id,
                state=item.state.value,
                commit=item.commit,
                chunks=item.chunks,
                indexed_at=item.indexed_at,
            )
            for item in response.projects
        ]
    )


@router.get("/audit-logs", response_model=AuditLogListResponse)
async def list_audit_logs(
    session: DbSession,
    _identity: Administrator,
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = None,
) -> AuditLogListResponse:
    offset = decode_cursor(cursor)
    rows = await AdminStore(session).list_audit_logs(limit=limit + 1, offset=offset)
    entries, next_cursor = paginate(rows, limit=limit, offset=offset)
    return AuditLogListResponse(
        items=[AuditLogResponse.model_validate(item) for item in entries],
        next_cursor=next_cursor,
    )


@router.get(
    "/repositories/{repository_id}/commits",
    response_model=AdminCommitListResponse,
)
async def list_repository_commits(
    repository_id: UUID,
    session: DbSession,
    _identity: Viewer,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    status: str | None = Query(default=None),
    branch_ref: str | None = Query(default=None),
    tag_ref: str | None = Query(default=None),
    change_request: str | None = Query(default=None),
) -> AdminCommitListResponse:
    try:
        await RepositoryStore(session).get(repository_id)
    except StoreLookupError as exc:
        raise ApiError(
            status_code=404,
            reason=exc.reason,
            detail=exc.detail,
            retryable=exc.retryable,
        ) from exc

    items, next_cursor, total = await AdminStore(session).list_repository_commits(
        repository_id,
        limit=limit,
        cursor=cursor,
        status=status,
        branch_ref=branch_ref,
        tag_ref=tag_ref,
        change_request=change_request,
    )
    return AdminCommitListResponse(
        ok=True,
        items=items,
        next_cursor=next_cursor,
        total_count=total,
    )


@router.get(
    "/repositories/{repository_id}/commits/{commit_sha}",
    response_model=AdminCommitDetailResponse,
)
async def get_repository_commit(
    repository_id: UUID,
    commit_sha: str,
    session: DbSession,
    _identity: Viewer,
) -> AdminCommitDetailResponse:
    try:
        await RepositoryStore(session).get(repository_id)
    except StoreLookupError as exc:
        raise ApiError(
            status_code=404,
            reason=exc.reason,
            detail=exc.detail,
            retryable=exc.retryable,
        ) from exc

    commit = await AdminStore(session).get_repository_commit(repository_id, commit_sha)
    if commit is None:
        raise ApiError(
            status_code=404,
            reason="COMMIT_NOT_FOUND",
            detail=f"Commit {commit_sha} was not found in catalog for repository {repository_id}.",
            retryable=False,
        )
    return AdminCommitDetailResponse(
        ok=True,
        commit=commit,
    )


@router.get(
    "/repositories/{repository_id}/compare",
    response_model=AdminCommitCompareResponse,
)
async def compare_repository_commits(
    request: Request,
    repository_id: UUID,
    session: DbSession,
    identity: Operator,
    base_revision: str = Query(..., min_length=40, max_length=40),
    target_revision: str = Query(..., min_length=40, max_length=40),
) -> AdminCommitCompareResponse:
    try:
        await RepositoryStore(session).get(repository_id)
    except StoreLookupError as exc:
        raise ApiError(
            status_code=404,
            reason=exc.reason,
            detail=exc.detail,
            retryable=exc.retryable,
        ) from exc

    git_client: RepositoryGitClient | None = getattr(
        request.app.state, "repository_git_client", None
    )
    if git_client is None:
        coll_svc = getattr(request.app.state, "repository_collection_service", None)
        if coll_svc is not None:
            git_client = getattr(coll_svc, "_git_client", None)
    if git_client is None:
        # Fallback to local cache directory if configured
        mat_root = getattr(request.app.state.settings, "snapshot_materialization_root", None)
        git_client = RepositoryGitClient(root=mat_root)

    try:
        compare_result = await run_in_threadpool(
            git_client.compare_revisions,
            repository_id=repository_id,
            base_revision=base_revision,
            target_revision=target_revision,
        )
    except CollectionError as exc:
        raise ApiError(
            status_code=exc.status_code,
            reason=exc.reason,
            detail=exc.detail,
            retryable=exc.retryable,
        ) from exc

    admin_store = AdminStore(session)
    base_status = await admin_store.get_revision_status(repository_id, base_revision)
    target_status = await admin_store.get_revision_status(repository_id, target_revision)

    await record_audit(
        session,
        request_id=identity.request_id,
        actor=identity.actor_id,
        action="compare_commits",
        target_type="repository",
        target_id=str(repository_id),
        outcome="succeeded",
        details={
            "base_revision": base_revision,
            "target_revision": target_revision,
            "files_changed": compare_result.files_changed,
            "additions": compare_result.additions,
            "deletions": compare_result.deletions,
        },
    )

    return AdminCommitCompareResponse(
        ok=True,
        repository_id=repository_id,
        base_revision=compare_result.base_revision,
        target_revision=compare_result.target_revision,
        merge_base_revision=compare_result.merge_base_revision,
        ahead_count=compare_result.ahead_count,
        behind_count=compare_result.behind_count,
        files_changed=compare_result.files_changed,
        additions=compare_result.additions,
        deletions=compare_result.deletions,
        changes=[
            AdminCommitCompareChangeItem(
                path=c.path,
                change_type=c.change_type,
                old_path=c.old_path,
            )
            for c in compare_result.changes
        ],
        base_status=base_status,
        target_status=target_status,
    )


@router.post(
    "/repositories/{repository_id}/commits/{commit_sha}/materialize",
    response_model=AdminCommitMaterializeResponse,
)
async def materialize_repository_commit(
    repository_id: UUID,
    commit_sha: str,
    request: Request,
    session: DbSession,
    identity: Operator,
    body: AdminCommitMaterializeRequest | None = None,
) -> AdminCommitMaterializeResponse:
    git_client: RepositoryGitClient | None = getattr(
        request.app.state, "repository_git_client", None
    )
    if git_client is None:
        raise ApiError(
            status_code=503,
            reason="REPOSITORY_GIT_CLIENT_UNAVAILABLE",
            detail="Repository Git client가 준비되지 않았습니다.",
            retryable=True,
        )

    materializer = getattr(
        request.app.state, "collected_revision_materializer", None
    )
    if materializer is None:
        raise ApiError(
            status_code=503,
            reason="MATERIALIZER_UNAVAILABLE",
            detail="Collected revision materializer가 준비되지 않았습니다.",
            retryable=True,
        )

    vss_project_id = body.vss_project_id if body else None
    branch_ref = body.branch_ref if body else None

    admin_store = AdminStore(session)
    result = await admin_store.materialize_commit(
        repository_id=repository_id,
        commit_sha=commit_sha,
        request_id=identity.request_id,
        vss_project_id=vss_project_id,
        branch_ref=branch_ref,
        materializer=materializer,
        git_client=git_client,
    )

    await record_audit(
        session,
        request_id=identity.request_id,
        actor=identity.actor_id,
        action="materialize_commit",
        target_type="repository",
        target_id=str(repository_id),
        outcome="succeeded",
        details={
            "commit_sha": commit_sha,
            "snapshot_id": str(result.snapshot_id),
            "created": result.created,
            "state": result.state,
            "materialized_locator": result.materialized_locator,
        },
    )

    return result

