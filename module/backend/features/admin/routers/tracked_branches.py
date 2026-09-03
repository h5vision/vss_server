"""Admin Tracked Branch routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy.exc import IntegrityError

from backend.core.errors import ApiError
from backend.features.admin.audit import record_audit
from backend.features.admin.common import (
    Administrator,
    DbSession,
    Viewer,
    _collection_error,
    _collection_service,
    _tracked_response,
)
from backend.features.admin.pagination import decode_cursor, paginate
from backend.features.admin.schemas import (
    AdminMutationResponse,
    BranchHeadHistoryItem,
    BranchHeadHistoryListResponse,
    TrackedBranchAdminListResponse,
    TrackedBranchAdminUpdateRequest,
)
from backend.features.admin.store import AdminStore
from backend.features.repository_collection.errors import CollectionError
from backend.features.repository_collection.schemas import TrackedBranchCreateRequest
from backend.features.repository_collection.store import RepositoryCollectionStore

router = APIRouter()


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
            detail=(
                "The requested Branch ref or VSS project is already tracked for this Repository."
            ),
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
