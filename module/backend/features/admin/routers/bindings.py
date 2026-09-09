"""Admin Branch Binding routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy.exc import IntegrityError

from backend.core.errors import ApiError
from backend.features.admin.audit import record_audit
from backend.features.admin.common import (
    Administrator,
    DbSession,
    Viewer,
    _binding_response,
    _not_found,
)
from backend.features.admin.pagination import decode_cursor, paginate
from backend.features.admin.schemas import AdminMutationResponse
from backend.features.repositories.schemas import (
    BranchBindingCreateRequest,
    BranchBindingListResponse,
    BranchBindingUpdateRequest,
)
from backend.features.repositories.store import (
    BranchBindingStore,
    StoreLookupError,
)

router = APIRouter()


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
