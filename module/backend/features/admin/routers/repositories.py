"""Admin Repository routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy.exc import IntegrityError

from backend.core.errors import ApiError
from backend.features.admin.audit import record_audit
from backend.features.admin.common import (
    Administrator,
    DbSession,
    Operator,
    Viewer,
    _collection_error,
    _collection_service,
    _not_found,
    _repository_response,
)
from backend.features.admin.pagination import decode_cursor, paginate
from backend.features.admin.schemas import (
    AdminMutationResponse,
    RepositorySyncRunItem,
    RepositorySyncRunListResponse,
)
from backend.features.admin.store import AdminStore
from backend.features.repositories.schemas import (
    RepositoryCreateRequest,
    RepositoryListResponse,
    RepositoryResponse,
    RepositoryUpdateRequest,
)
from backend.features.repositories.store import (
    RepositoryStore,
    StoreLookupError,
)
from backend.features.repository_collection.errors import CollectionError
from backend.features.repository_collection.schemas import RepositoryCatalogResult

router = APIRouter()


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
            reason="REPOSITORY_ALREADY_EXISTS",
            detail="A Repository with the same canonical name or remote URL already exists.",
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
        detail="Repository configuration was updated.",
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
async def list_remote_branches(
    repository_id: UUID,
    request: Request,
    _identity: Viewer,
) -> RepositoryCatalogResult:
    service = _collection_service(request)
    try:
        return await service.catalog_repository(repository_id)
    except CollectionError as exc:
        raise _collection_error(exc) from exc


@router.post(
    "/repositories/{repository_id}/sync",
    response_model=AdminMutationResponse,
)
async def sync_repository(
    repository_id: UUID,
    request: Request,
    session: DbSession,
    identity: Operator,
) -> AdminMutationResponse:
    service = _collection_service(request)
    try:
        result = await service.sync_repository(
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
