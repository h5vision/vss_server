"""FastAPI dependencies for Admin Use Cases and infrastructure services."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from backend.core.errors import ApiError
from backend.features.admin.common import DbSession
from backend.features.admin.use_cases.compare_revisions import CompareRevisionsUseCase
from backend.features.admin.use_cases.materialize_commit import MaterializeCommitUseCase
from backend.features.repository_collection.git_client import RepositoryGitClient
from backend.features.repository_collection.materializer import (
    CollectedRevisionMaterializer,
)
from backend.ports.git import RevisionComparator


def get_repository_git_client(request: Request) -> RepositoryGitClient:
    git_client: RepositoryGitClient | None = getattr(
        request.app.state, "repository_git_client", None
    )
    if git_client is not None:
        return git_client

    container = getattr(request.app.state, "container", None)
    if container is not None and container.repository_git_client is not None:
        return container.repository_git_client

    cache_root = getattr(request.app.state, "bare_cache_root_path", None)
    if cache_root:
        return RepositoryGitClient(root=cache_root)

    raise ApiError(
        status_code=503,
        reason="REPOSITORY_GIT_CLIENT_UNAVAILABLE",
        detail="Repository Git client가 준비되지 않았습니다.",
        retryable=True,
    )


def get_revision_materializer(request: Request) -> CollectedRevisionMaterializer:
    materializer: CollectedRevisionMaterializer | None = getattr(
        request.app.state, "collected_revision_materializer", None
    )
    if materializer is not None:
        return materializer

    container = getattr(request.app.state, "container", None)
    if container is not None and container.collected_revision_materializer is not None:
        return container.collected_revision_materializer

    raise ApiError(
        status_code=503,
        reason="MATERIALIZER_UNAVAILABLE",
        detail="Collected revision materializer가 준비되지 않았습니다.",
        retryable=True,
    )


def get_compare_revisions_use_case(
    request: Request,
    session: DbSession,
) -> CompareRevisionsUseCase:
    comparator: RevisionComparator = get_repository_git_client(request)
    return CompareRevisionsUseCase(comparator=comparator, session=session)


def get_materialize_commit_use_case(
    request: Request,
    session: DbSession,
) -> MaterializeCommitUseCase:
    git_client = get_repository_git_client(request)
    materializer = get_revision_materializer(request)
    return MaterializeCommitUseCase(
        materializer=materializer,
        git_client=git_client,
        session=session,
    )


CompareRevisionsDep = Annotated[
    CompareRevisionsUseCase,
    Depends(get_compare_revisions_use_case),
]
MaterializeCommitDep = Annotated[
    MaterializeCommitUseCase,
    Depends(get_materialize_commit_use_case),
]
