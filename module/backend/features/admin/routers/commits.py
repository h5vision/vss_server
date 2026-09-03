"""Admin Commit catalog, comparison, and materialization routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, Request
from starlette.concurrency import run_in_threadpool

from backend.core.errors import ApiError
from backend.features.admin.audit import record_audit
from backend.features.admin.common import (
    DbSession,
    Operator,
    Viewer,
)
from backend.features.admin.schemas import (
    AdminCommitCompareChangeItem,
    AdminCommitCompareResponse,
    AdminCommitDetailResponse,
    AdminCommitListResponse,
    AdminCommitMaterializeRequest,
    AdminCommitMaterializeResponse,
)
from backend.features.admin.store import AdminStore
from backend.features.repositories.store import RepositoryStore, StoreLookupError
from backend.features.repository_collection.git_client import RepositoryGitClient

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
    request: Request,
    session: DbSession,
    identity: Operator,
) -> AdminCommitCompareResponse:
    try:
        await RepositoryStore(session).get(repository_id)
    except StoreLookupError as exc:
        raise ApiError(
            status_code=404,
            reason=exc.reason,
            detail=exc.detail,
            retryable=False,
        ) from exc

    git_client: RepositoryGitClient | None = getattr(
        request.app.state, "repository_git_client", None
    )
    if git_client is None:
        coll_svc = getattr(request.app.state, "repository_collection_service", None)
        git_client = getattr(coll_svc, "_git_client", None) if coll_svc else None

    if git_client is None:
        cache_root = getattr(request.app.state, "bare_cache_root_path", None)
        if cache_root:
            git_client = RepositoryGitClient(root=cache_root)
        else:
            raise ApiError(
                status_code=503,
                reason="REPOSITORY_GIT_CLIENT_UNAVAILABLE",
                detail="Repository Git client가 준비되지 않았습니다.",
                retryable=True,
            )

    try:
        compare_result = await run_in_threadpool(
            git_client.compare_revisions,
            repository_id=repository_id,
            base_revision=base_revision,
            target_revision=target_revision,
        )
    except Exception as exc:
        raise ApiError(
            status_code=400,
            reason="COMPARE_FAILED",
            detail=f"커밋 비교에 실패했습니다: {exc}",
            retryable=False,
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
