"""Admin Commit catalog, comparison, and materialization routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query

from backend.core.errors import ApiError
from backend.features.admin.common import (
    DbSession,
    Operator,
    Viewer,
)
from backend.features.admin.dependencies import (
    CompareRevisionsDep,
    MaterializeCommitDep,
)
from backend.features.admin.schemas import (
    AdminCommitCompareResponse,
    AdminCommitDetailResponse,
    AdminCommitListResponse,
    AdminCommitMaterializeRequest,
    AdminCommitMaterializeResponse,
)
from backend.features.admin.store import AdminStore
from backend.features.repositories.store import RepositoryStore, StoreLookupError

router = APIRouter()


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
            retryable=False,
        ) from exc

    commits, next_cursor, _ = await AdminStore(session).list_repository_commits(
        repository_id,
        limit=limit,
        cursor=cursor,
        status=status,
        branch_ref=branch_ref,
        tag_ref=tag_ref,
        change_request=change_request,
    )
    return AdminCommitListResponse(
        items=commits,
        next_cursor=next_cursor,
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
            retryable=False,
        ) from exc

    commit = await AdminStore(session).get_repository_commit(repository_id, commit_sha)
    if commit is None:
        raise ApiError(
            status_code=404,
            reason="COMMIT_NOT_FOUND",
            detail="요청한 커밋을 찾을 수 없습니다.",
            retryable=False,
        )

    return AdminCommitDetailResponse(ok=True, commit=commit)


@router.get(
    "/repositories/{repository_id}/compare",
    response_model=AdminCommitCompareResponse,
)
async def compare_repository_commits(
    repository_id: UUID,
    base_revision: str,
    target_revision: str,
    identity: Operator,
    use_case: CompareRevisionsDep,
) -> AdminCommitCompareResponse:
    return await use_case.execute(
        repository_id=repository_id,
        base_revision=base_revision,
        target_revision=target_revision,
        actor_id=identity.actor_id,
        request_id=identity.request_id,
    )


@router.post(
    "/repositories/{repository_id}/commits/{commit_sha}/materialize",
    response_model=AdminCommitMaterializeResponse,
)
async def materialize_repository_commit(
    repository_id: UUID,
    commit_sha: str,
    identity: Operator,
    use_case: MaterializeCommitDep,
    body: AdminCommitMaterializeRequest | None = None,
) -> AdminCommitMaterializeResponse:
    return await use_case.execute(
        repository_id=repository_id,
        commit_sha=commit_sha,
        actor_id=identity.actor_id,
        request_id=identity.request_id,
        vss_project_id=body.vss_project_id if body else None,
        branch_ref=body.branch_ref if body else None,
    )
