"""DB Snapshot과 immutable Git tree를 VSS용 검증 descriptor로 변환한다."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from backend.core.errors import ApiError
from backend.core.orchestration import MODULE_PUSH, IndexOrchestrationMode
from backend.features.materialization.errors import MaterializationError
from backend.features.materialization.service import SnapshotMaterializer
from backend.features.repository_collection.errors import CollectionError
from backend.features.snapshots.store import SnapshotStore
from backend.features.vss_sources.schemas import (
    GitSourceVerification,
    VssCommitContext,
    VssCommitGraphResponse,
    VssContextResponse,
    VssContextSelection,
    VssDeltaChange,
    VssDeltaResponse,
    VssPullCapabilitiesResponse,
    VssReferenceItem,
    VssReferenceListResponse,
    VssRepositoryBranchItem,
    VssRepositoryItem,
    VssRepositoryListResponse,
    VssRevisionItem,
    VssRevisionListResponse,
    VssSnapshotReadiness,
    VssSourceDescriptorResponse,
)
from backend.infrastructure.database.models import (
    BranchBinding,
    CommitCatalogRun,
    Repository,
    RepositoryCommit,
    RepositoryCommitParent,
    Snapshot,
    TrackedBranch,
)
from backend.integrations.vss.schemas import VssIndexRequest
from backend.ports.git import RevisionComparator


class VssSourceService:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        materializer: SnapshotMaterializer,
        git_timeout_seconds: float,
        revision_comparator: RevisionComparator | None = None,
        index_orchestration_mode: IndexOrchestrationMode = MODULE_PUSH,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._materializer = materializer
        self._git_timeout_seconds = git_timeout_seconds
        self._revision_comparator = revision_comparator
        self._index_orchestration_mode = index_orchestration_mode

    def capabilities(self, *, request_id: UUID) -> VssPullCapabilitiesResponse:
        module_starts_indexing = self._index_orchestration_mode == MODULE_PUSH
        return VssPullCapabilitiesResponse(
            detail="VSS가 사용할 수 있는 Snapshot pull 계약과 인덱싱 시작 소유권입니다.",
            request_id=request_id,
            orchestration_mode=self._index_orchestration_mode,
            index_start_owner="module" if module_starts_indexing else "vss",
            module_starts_indexing=module_starts_indexing,
            resources=[
                "source",
                "revisions",
                "refs",
                "context",
                "repositories",
                "commit_graph",
                "delta",
            ],
            context_selectors=["revision", "branch"],
        )

    async def repositories(self, *, request_id: UUID) -> VssRepositoryListResponse:
        async with self._sessionmaker() as session:
            try:
                repositories = list(
                    await session.scalars(
                        select(Repository)
                        .where(Repository.active.is_(True))
                        .order_by(Repository.canonical_name)
                    )
                )
                repository_ids = [item.repository_id for item in repositories]
                branches: list[TrackedBranch] = []
                if repository_ids:
                    branches = list(
                        await session.scalars(
                            select(TrackedBranch)
                            .where(
                                TrackedBranch.repository_id.in_(repository_ids),
                                TrackedBranch.tracked.is_(True),
                            )
                            .order_by(TrackedBranch.repository_id, TrackedBranch.branch_ref)
                        )
                    )
            except SQLAlchemyError as exc:
                raise self._database_unavailable() from exc

        branches_by_repository: dict[UUID, list[TrackedBranch]] = {}
        for branch in branches:
            branches_by_repository.setdefault(branch.repository_id, []).append(branch)
        return VssRepositoryListResponse(
            detail="Module이 관리하는 Repository와 tracked Branch HEAD 목록입니다.",
            request_id=request_id,
            items=[
                VssRepositoryItem(
                    repository_id=repository.repository_id,
                    repository_name=repository.canonical_name,
                    display_name=repository.display_name,
                    provider=repository.provider,
                    default_branch_ref=repository.default_branch_ref,
                    branches=self._repository_branch_items(
                        repository,
                        branches_by_repository.get(repository.repository_id, []),
                    ),
                )
                for repository in repositories
            ],
        )

    async def commit_graph(
        self,
        repository_id: UUID,
        *,
        limit: int,
        cursor: str | None,
        request_id: UUID,
    ) -> VssCommitGraphResponse:
        async with self._sessionmaker() as session:
            try:
                repository = await session.get(Repository, repository_id)
                if repository is None or not repository.active:
                    raise ApiError(
                        status_code=404,
                        reason="VSS_REPOSITORY_NOT_FOUND",
                        detail="요청한 active Repository를 찾을 수 없습니다.",
                        retryable=False,
                    )
                branches = list(
                    await session.scalars(
                        select(TrackedBranch)
                        .where(
                            TrackedBranch.repository_id == repository_id,
                            TrackedBranch.tracked.is_(True),
                        )
                        .order_by(TrackedBranch.branch_ref)
                    )
                )
                latest_run = await session.scalar(
                    select(CommitCatalogRun)
                    .where(CommitCatalogRun.repository_id == repository_id)
                    .order_by(CommitCatalogRun.started_at.desc())
                    .limit(1)
                )
                statement = select(RepositoryCommit).where(
                    RepositoryCommit.repository_id == repository_id
                )
                if cursor is not None:
                    cursor_commit = await session.scalar(
                        select(RepositoryCommit).where(
                            RepositoryCommit.repository_id == repository_id,
                            RepositoryCommit.commit_sha == cursor,
                        )
                    )
                    if cursor_commit is None:
                        raise ApiError(
                            status_code=404,
                            reason="VSS_COMMIT_CURSOR_NOT_FOUND",
                            detail="요청한 commit cursor가 Repository catalog에 없습니다.",
                            retryable=False,
                        )
                    statement = statement.where(
                        or_(
                            RepositoryCommit.committed_at < cursor_commit.committed_at,
                            (RepositoryCommit.committed_at == cursor_commit.committed_at)
                            & (RepositoryCommit.commit_sha < cursor_commit.commit_sha),
                        )
                    )
                commits = list(
                    await session.scalars(
                        statement.order_by(
                            RepositoryCommit.committed_at.desc(),
                            RepositoryCommit.commit_sha.desc(),
                        ).limit(limit + 1)
                    )
                )
                has_next = len(commits) > limit
                if has_next:
                    commits = commits[:limit]
                commit_ids = [item.repository_commit_id for item in commits]
                parents: list[RepositoryCommitParent] = []
                if commit_ids:
                    parents = list(
                        await session.scalars(
                            select(RepositoryCommitParent)
                            .where(RepositoryCommitParent.repository_commit_id.in_(commit_ids))
                            .order_by(
                                RepositoryCommitParent.repository_commit_id,
                                RepositoryCommitParent.parent_order,
                            )
                        )
                    )
            except ApiError:
                raise
            except SQLAlchemyError as exc:
                raise self._database_unavailable() from exc

        parents_by_commit: dict[UUID, list[str]] = {}
        for parent in parents:
            parents_by_commit.setdefault(parent.repository_commit_id, []).append(parent.parent_sha)
        return VssCommitGraphResponse(
            detail=(
                "Module commit catalog의 parent edge와 tracked Branch HEAD를 VSS가 읽을 수 있는 "
                "Git commit graph입니다."
            ),
            request_id=request_id,
            repository_id=repository.repository_id,
            repository_name=repository.canonical_name,
            default_branch_ref=repository.default_branch_ref,
            branches=self._repository_branch_items(repository, branches),
            catalog_state=latest_run.state if latest_run is not None else None,
            history_complete=(
                latest_run.history_complete if latest_run is not None else None
            ),
            truncated=latest_run.truncated if latest_run is not None else None,
            shallow=latest_run.shallow if latest_run is not None else None,
            items=[
                VssCommitContext(
                    commit_sha=commit.commit_sha,
                    tree_sha=commit.tree_sha,
                    parent_shas=parents_by_commit.get(commit.repository_commit_id, []),
                    author_name=commit.author_name,
                    authored_at=commit.authored_at,
                    committed_at=commit.committed_at,
                    subject=commit.subject,
                )
                for commit in commits
            ],
            next_cursor=commits[-1].commit_sha if has_next and commits else None,
        )

    async def delta(
        self,
        project_id: str,
        *,
        base_revision: str,
        target_revision: str,
        request_id: UUID,
    ) -> VssDeltaResponse:
        normalized_project_id = project_id.strip()
        base = base_revision.lower()
        target = target_revision.lower()
        if self._revision_comparator is None:
            raise ApiError(
                status_code=503,
                reason="VSS_DELTA_NOT_CONFIGURED",
                detail="Repository Git comparator가 구성되지 않았습니다.",
                retryable=True,
            )

        async with self._sessionmaker() as session:
            try:
                repository = await self._repository_for_project(session, normalized_project_id)
                branch_ref = await self._branch_ref_for_project(
                    session,
                    normalized_project_id,
                    repository.repository_id,
                )
            except ApiError:
                raise
            except SQLAlchemyError as exc:
                raise self._database_unavailable() from exc

        try:
            compared = await run_in_threadpool(
                self._revision_comparator.compare_revisions,
                repository_id=repository.repository_id,
                base_revision=base,
                target_revision=target,
            )
        except CollectionError as exc:
            if exc.reason in {
                "REPOSITORY_CACHE_UNAVAILABLE",
                "COMPARE_REVISION_NOT_FOUND",
                "COMPARE_GIT_FAILED",
                "COMPARE_CHANGES_LIMIT_EXCEEDED",
            }:
                return VssDeltaResponse(
                    reason="VSS_DELTA_FULL_REINDEX_REQUIRED",
                    detail=(
                        "Exact Git delta를 안전하게 만들 수 없어 full reindex가 필요합니다."
                    ),
                    request_id=request_id,
                    project_id=normalized_project_id,
                    repository_id=repository.repository_id,
                    repository_name=repository.canonical_name,
                    branch_ref=branch_ref,
                    base_revision=base,
                    target_revision=target,
                    relationship="unknown",
                    delta_complete=False,
                    full_reindex_required=True,
                    fallback_reason=exc.reason,
                    ahead_count=0,
                    behind_count=0,
                    files_changed=0,
                    additions=0,
                    deletions=0,
                    changes=[],
                )
            raise ApiError(
                status_code=exc.status_code,
                reason=exc.reason,
                detail=exc.detail,
                retryable=exc.retryable,
            ) from exc

        if compared.base_tree_sha is None or compared.target_tree_sha is None:
            return VssDeltaResponse(
                reason="VSS_DELTA_FULL_REINDEX_REQUIRED",
                detail="Git tree SHA 증거가 없어 full reindex가 필요합니다.",
                request_id=request_id,
                project_id=normalized_project_id,
                repository_id=repository.repository_id,
                repository_name=repository.canonical_name,
                branch_ref=branch_ref,
                base_revision=base,
                target_revision=target,
                merge_base_revision=compared.merge_base_revision,
                relationship="unknown",
                delta_complete=False,
                full_reindex_required=True,
                fallback_reason="TREE_SHA_UNAVAILABLE",
                ahead_count=compared.ahead_count,
                behind_count=compared.behind_count,
                files_changed=compared.files_changed,
                additions=compared.additions,
                deletions=compared.deletions,
                changes=[],
            )

        if base == target:
            relationship = "same"
            full_reindex_required = False
        elif compared.merge_base_revision == base:
            relationship = "fast_forward"
            full_reindex_required = False
        elif compared.merge_base_revision is None:
            relationship = "unknown"
            full_reindex_required = True
        else:
            relationship = "diverged"
            full_reindex_required = True

        if full_reindex_required:
            return VssDeltaResponse(
                reason="VSS_DELTA_FULL_REINDEX_REQUIRED",
                detail="base revision이 target의 안전한 fast-forward 기준이 아닙니다.",
                request_id=request_id,
                project_id=normalized_project_id,
                repository_id=repository.repository_id,
                repository_name=repository.canonical_name,
                branch_ref=branch_ref,
                base_revision=base,
                target_revision=target,
                base_tree_sha=compared.base_tree_sha,
                target_tree_sha=compared.target_tree_sha,
                merge_base_revision=compared.merge_base_revision,
                relationship=relationship,
                delta_complete=False,
                full_reindex_required=True,
                fallback_reason=(
                    "NO_COMMON_ANCESTOR" if relationship == "unknown" else "NON_FAST_FORWARD"
                ),
                ahead_count=compared.ahead_count,
                behind_count=compared.behind_count,
                files_changed=compared.files_changed,
                additions=compared.additions,
                deletions=compared.deletions,
                changes=[],
            )

        changes = [
            VssDeltaChange(
                status=("added" if item.change_type == "copied" else item.change_type),
                path=item.path,
                old_path=item.old_path if item.change_type == "renamed" else None,
            )
            for item in compared.changes
        ]
        return VssDeltaResponse(
            reason="VSS_DELTA_READY",
            detail="Exact Git base-to-target delta가 증분 인덱싱에 사용 가능합니다.",
            request_id=request_id,
            project_id=normalized_project_id,
            repository_id=repository.repository_id,
            repository_name=repository.canonical_name,
            branch_ref=branch_ref,
            base_revision=base,
            target_revision=target,
            base_tree_sha=compared.base_tree_sha,
            target_tree_sha=compared.target_tree_sha,
            merge_base_revision=compared.merge_base_revision,
            relationship=relationship,
            delta_complete=True,
            full_reindex_required=False,
            ahead_count=compared.ahead_count,
            behind_count=compared.behind_count,
            files_changed=compared.files_changed,
            additions=compared.additions,
            deletions=compared.deletions,
            changes=changes,
        )

    async def describe(
        self,
        project_id: str,
        *,
        revision: str | None,
        request_id: UUID,
    ) -> VssSourceDescriptorResponse:
        normalized_project_id = project_id.strip()
        normalized_revision = revision.lower() if revision is not None else None
        async with self._sessionmaker() as session:
            try:
                snapshot = await SnapshotStore(session).source_for_vss_project(
                    normalized_project_id,
                    revision=normalized_revision,
                )
                if snapshot is None:
                    raise ApiError(
                        status_code=404,
                        reason="VSS_SOURCE_NOT_FOUND",
                        detail="요청한 VSS project와 revision에 게시 가능한 Snapshot이 없습니다.",
                        retryable=False,
                    )
                repository = await session.get(Repository, snapshot.repository_id)
            except ApiError:
                raise
            except SQLAlchemyError as exc:
                raise self._database_unavailable() from exc

            if repository is None or not repository.active:
                raise ApiError(
                    status_code=409,
                    reason="VSS_SOURCE_REPOSITORY_INACTIVE",
                    detail="Snapshot의 Repository가 없거나 비활성 상태입니다.",
                    retryable=False,
                )
            if snapshot.materialized_locator is None:
                raise ApiError(
                    status_code=409,
                    reason="VSS_SOURCE_NOT_MATERIALIZED",
                    detail="Snapshot의 immutable revision 디렉터리가 준비되지 않았습니다.",
                    retryable=True,
                )

            try:
                tree = await run_in_threadpool(
                    self._materializer.verify_existing,
                    snapshot.materialized_locator,
                    snapshot.target_revision,
                )
                verification = await run_in_threadpool(
                    self._read_git_verification,
                    tree.project_root,
                    snapshot.target_revision,
                )
            except MaterializationError as exc:
                raise ApiError(
                    status_code=409,
                    reason=exc.reason,
                    detail="게시할 Snapshot의 Git 정합성 검증에 실패했습니다.",
                    retryable=exc.retryable,
                ) from exc

            index_request = VssIndexRequest(
                project_root=str(tree.project_root),
                project_id=snapshot.vss_project_id,
                force=False,
                briefing=True,
                note=f"snapshot {snapshot.target_revision}",
            )
            return VssSourceDescriptorResponse(
                detail="VSS가 독립 검증 후 인덱싱할 수 있는 Snapshot 소스입니다.",
                request_id=request_id,
                project_id=snapshot.vss_project_id,
                repository_id=repository.repository_id,
                repository_name=repository.canonical_name,
                branch_ref=snapshot.branch_ref,
                snapshot_id=snapshot.snapshot_id,
                snapshot_state=snapshot.state,
                source_type=snapshot.source_type,
                base_revision=snapshot.base_revision,
                target_revision=snapshot.target_revision,
                verification=verification,
                index_request=index_request,
            )

    async def revisions(
        self,
        project_id: str,
        *,
        limit: int,
        request_id: UUID,
    ) -> VssRevisionListResponse:
        normalized_project_id = project_id.strip()
        async with self._sessionmaker() as session:
            try:
                snapshots = await SnapshotStore(session).revisions_for_vss_project(
                    normalized_project_id,
                    limit=limit,
                )
            except SQLAlchemyError as exc:
                raise self._database_unavailable() from exc
        return VssRevisionListResponse(
            detail="VSS project에 연결된 Snapshot revision 이력입니다.",
            request_id=request_id,
            project_id=normalized_project_id,
            items=[
                VssRevisionItem(
                    snapshot_id=item.snapshot_id,
                    repository_id=item.repository_id,
                    branch_ref=item.branch_ref,
                    base_revision=item.base_revision,
                    target_revision=item.target_revision,
                    snapshot_state=item.state,
                    materialized=item.materialized_locator is not None,
                    vss_state=item.vss_state,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                )
                for item in snapshots
            ],
        )

    async def refs(
        self,
        project_id: str,
        *,
        request_id: UUID,
    ) -> VssReferenceListResponse:
        normalized_project_id = project_id.strip()
        async with self._sessionmaker() as session:
            try:
                repository = await self._repository_for_project(session, normalized_project_id)
                branches = list(
                    await session.scalars(
                        select(TrackedBranch)
                        .where(
                            TrackedBranch.repository_id == repository.repository_id,
                            TrackedBranch.tracked.is_(True),
                            TrackedBranch.current_head_sha.is_not(None),
                        )
                        .order_by(TrackedBranch.branch_ref)
                    )
                )
                revisions = {
                    branch.current_head_sha for branch in branches if branch.current_head_sha
                }
                snapshots = []
                if revisions:
                    snapshots = list(
                        await session.scalars(
                            select(Snapshot)
                            .where(
                                Snapshot.repository_id == repository.repository_id,
                                Snapshot.target_revision.in_(revisions),
                            )
                            .order_by(Snapshot.updated_at.desc())
                        )
                    )
            except ApiError:
                raise
            except SQLAlchemyError as exc:
                raise self._database_unavailable() from exc

        snapshots_by_project_revision: dict[tuple[str, str], Snapshot] = {}
        for snapshot in snapshots:
            snapshots_by_project_revision.setdefault(
                (snapshot.vss_project_id, snapshot.target_revision), snapshot
            )

        items = [
            VssReferenceItem(
                kind="branch",
                ref=branch.branch_ref,
                revision=branch.current_head_sha,
                project_id=branch.vss_project_id,
                is_default=branch.branch_ref == repository.default_branch_ref,
                observed_at=branch.last_fetched_at,
                readiness=self._snapshot_readiness(
                    snapshots_by_project_revision.get(
                        (branch.vss_project_id, branch.current_head_sha)
                    )
                ),
            )
            for branch in branches
            if branch.current_head_sha is not None
        ]
        return VssReferenceListResponse(
            detail="현재 관측된 Branch와 Tag의 exact commit 및 Snapshot 준비 상태입니다.",
            request_id=request_id,
            project_id=normalized_project_id,
            repository_id=repository.repository_id,
            repository_name=repository.canonical_name,
            orchestration_mode=self._index_orchestration_mode,
            items=items,
        )

    async def context(
        self,
        project_id: str,
        *,
        revision: str | None,
        branch_ref: str | None,
        request_id: UUID,
    ) -> VssContextResponse:
        normalized_project_id = project_id.strip()
        async with self._sessionmaker() as session:
            try:
                repository = await self._repository_for_project(session, normalized_project_id)
                selected_revision, selection = await self._resolve_context_selection(
                    session,
                    repository,
                    revision=revision,
                    branch_ref=branch_ref,
                )
                commit = await session.scalar(
                    select(RepositoryCommit).where(
                        RepositoryCommit.repository_id == repository.repository_id,
                        RepositoryCommit.commit_sha == selected_revision,
                    )
                )
                snapshot = await session.scalar(
                    select(Snapshot)
                    .where(
                        Snapshot.repository_id == repository.repository_id,
                        Snapshot.vss_project_id == normalized_project_id,
                        Snapshot.target_revision == selected_revision,
                    )
                    .order_by(Snapshot.updated_at.desc())
                    .limit(1)
                )
                if selection.kind == "revision" and commit is None and snapshot is None:
                    raise ApiError(
                        status_code=404,
                        reason="VSS_CONTEXT_REVISION_NOT_FOUND",
                        detail="요청한 commit이 Repository catalog 또는 Snapshot에 없습니다.",
                        retryable=False,
                    )
                parent_shas = []
                if commit is not None:
                    parent_shas = list(
                        await session.scalars(
                            select(RepositoryCommitParent.parent_sha)
                            .where(
                                RepositoryCommitParent.repository_commit_id
                                == commit.repository_commit_id
                            )
                            .order_by(RepositoryCommitParent.parent_order)
                        )
                    )
            except ApiError:
                raise
            except SQLAlchemyError as exc:
                raise self._database_unavailable() from exc

        commit_context = None
        if commit is not None:
            commit_context = VssCommitContext(
                commit_sha=commit.commit_sha,
                tree_sha=commit.tree_sha,
                parent_shas=parent_shas,
                author_name=commit.author_name,
                authored_at=commit.authored_at,
                committed_at=commit.committed_at,
                subject=commit.subject,
            )
        return VssContextResponse(
            detail=(
                "selector를 Repository 관측값에 exact match하여 VSS가 소비할 revision을 "
                "결정했습니다."
            ),
            request_id=request_id,
            project_id=normalized_project_id,
            repository_id=repository.repository_id,
            repository_name=repository.canonical_name,
            orchestration_mode=self._index_orchestration_mode,
            selection=selection,
            selected_revision=selected_revision,
            commit=commit_context,
            readiness=self._snapshot_readiness(snapshot),
        )

    async def _resolve_context_selection(
        self,
        session: AsyncSession,
        repository: Repository,
        *,
        revision: str | None,
        branch_ref: str | None,
    ) -> tuple[str, VssContextSelection]:
        if revision is not None:
            return revision, VssContextSelection(
                kind="revision",
                value=revision,
                reason="EXACT_REVISION",
            )
        if branch_ref is not None:
            branch = await session.scalar(
                select(TrackedBranch).where(
                    TrackedBranch.repository_id == repository.repository_id,
                    TrackedBranch.branch_ref == branch_ref,
                    TrackedBranch.tracked.is_(True),
                )
            )
            if branch is None:
                raise self._context_ref_not_found("Branch")
            if branch.current_head_sha is None:
                raise self._context_ref_unavailable("Branch")
            return branch.current_head_sha, VssContextSelection(
                kind="branch",
                value=branch_ref,
                reason="BRANCH_HEAD",
            )
        raise ApiError(
            status_code=422,
            reason="VSS_CONTEXT_SELECTOR_INVALID",
            detail="revision ?? branch_ref ? ??? ??? ?????.",
            retryable=False,
        )

    async def _branch_ref_for_project(
        self,
        session: AsyncSession,
        project_id: str,
        repository_id: UUID,
    ) -> str:
        branch_ref = await session.scalar(
            select(TrackedBranch.branch_ref).where(
                TrackedBranch.repository_id == repository_id,
                TrackedBranch.vss_project_id == project_id,
                TrackedBranch.tracked.is_(True),
            )
        )
        if branch_ref is None:
            branch_ref = await session.scalar(
                select(BranchBinding.branch_ref).where(
                    BranchBinding.repository_id == repository_id,
                    BranchBinding.vss_project_id == project_id,
                    BranchBinding.active.is_(True),
                )
            )
        if branch_ref is None:
            branch_ref = await session.scalar(
                select(Snapshot.branch_ref)
                .where(
                    Snapshot.repository_id == repository_id,
                    Snapshot.vss_project_id == project_id,
                )
                .order_by(Snapshot.updated_at.desc())
                .limit(1)
            )
        if branch_ref is None:
            raise ApiError(
                status_code=404,
                reason="VSS_CONTEXT_PROJECT_NOT_FOUND",
                detail="요청한 VSS project의 Branch reference를 찾을 수 없습니다.",
                retryable=False,
            )
        return branch_ref

    async def _repository_for_project(self, session, project_id: str) -> Repository:
        repository_id = await session.scalar(
            select(TrackedBranch.repository_id).where(
                TrackedBranch.vss_project_id == project_id,
                TrackedBranch.tracked.is_(True),
            )
        )
        if repository_id is None:
            repository_id = await session.scalar(
                select(BranchBinding.repository_id).where(
                    BranchBinding.vss_project_id == project_id,
                    BranchBinding.active.is_(True),
                )
            )
        if repository_id is None:
            repository_id = await session.scalar(
                select(Snapshot.repository_id)
                .where(Snapshot.vss_project_id == project_id)
                .order_by(Snapshot.updated_at.desc())
                .limit(1)
            )
        repository = (
            await session.get(Repository, repository_id) if repository_id is not None else None
        )
        if repository is None or not repository.active:
            raise ApiError(
                status_code=404,
                reason="VSS_CONTEXT_PROJECT_NOT_FOUND",
                detail="요청한 VSS project에 연결된 활성 Repository가 없습니다.",
                retryable=False,
            )
        return repository

    @staticmethod
    def _snapshot_readiness(snapshot: Snapshot | None) -> VssSnapshotReadiness:
        if snapshot is None:
            return VssSnapshotReadiness(
                source_unavailable_reason="SNAPSHOT_NOT_FOUND",
                index_unavailable_reason="SNAPSHOT_NOT_FOUND",
            )
        materialized = snapshot.materialized_locator is not None
        index_ready_observed = (
            snapshot.state in {"completed", "already_indexed"}
            and snapshot.vss_state == "done"
        )
        return VssSnapshotReadiness(
            snapshot_id=snapshot.snapshot_id,
            snapshot_state=snapshot.state,
            materialized=materialized,
            source_ready=materialized,
            vss_state=snapshot.vss_state,
            index_ready_observed=index_ready_observed,
            source_unavailable_reason=None if materialized else "SNAPSHOT_NOT_MATERIALIZED",
            index_unavailable_reason=(
                None if index_ready_observed else "INDEX_NOT_OBSERVED_READY"
            ),
        )

    @staticmethod
    def _repository_branch_items(
        repository: Repository,
        branches: list[TrackedBranch],
    ) -> list[VssRepositoryBranchItem]:
        return [
            VssRepositoryBranchItem(
                tracked_branch_id=branch.tracked_branch_id,
                branch_ref=branch.branch_ref,
                project_id=branch.vss_project_id,
                current_head_sha=branch.current_head_sha,
                is_default=branch.branch_ref == repository.default_branch_ref,
                observed_at=branch.last_fetched_at,
            )
            for branch in branches
        ]

    @staticmethod
    def _context_ref_not_found(kind: str) -> ApiError:
        return ApiError(
            status_code=404,
            reason="VSS_CONTEXT_REF_NOT_FOUND",
            detail=f"요청한 {kind} reference를 찾을 수 없습니다.",
            retryable=False,
        )

    @staticmethod
    def _context_ref_unavailable(kind: str) -> ApiError:
        return ApiError(
            status_code=409,
            reason="VSS_CONTEXT_REF_UNAVAILABLE",
            detail=f"요청한 {kind} reference에는 현재 commit이 없습니다.",
            retryable=False,
        )

    def _read_git_verification(
        self,
        project_root: Path,
        expected_revision: str,
    ) -> GitSourceVerification:
        commit = self._git_output(project_root, "rev-parse", "HEAD")
        tree = self._git_output(project_root, "rev-parse", "HEAD^{tree}")
        object_format = self._git_output(project_root, "rev-parse", "--show-object-format")
        status = self._git_output(
            project_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        if commit.lower() != expected_revision.lower() or status or object_format != "sha1":
            raise MaterializationError(
                reason="SNAPSHOT_REVISION_MISMATCH",
                detail="immutable Git tree의 commit, object format 또는 working tree가 다릅니다.",
                status_code=409,
                retryable=False,
            )
        return GitSourceVerification(
            expected_commit_sha=commit.lower(),
            expected_tree_sha=tree.lower(),
            object_format="sha1",
            git_metadata_present=True,
            working_tree_clean=True,
            verified_at=datetime.now(timezone.utc),
            verification_commands=[
                "git rev-parse HEAD",
                "git rev-parse HEAD^{tree}",
                "git status --porcelain=v1 --untracked-files=all",
            ],
        )

    def _git_output(self, project_root: Path, *arguments: str) -> str:
        environment = os.environ.copy()
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        environment["GIT_CONFIG_GLOBAL"] = os.devnull
        environment.pop("GIT_DIR", None)
        environment.pop("GIT_WORK_TREE", None)
        try:
            result = subprocess.run(
                ["git", "-C", str(project_root), *arguments],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._git_timeout_seconds,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise MaterializationError(
                reason="SNAPSHOT_MATERIALIZATION_FAILED",
                detail="immutable Git tree의 검증 값을 읽지 못했습니다.",
                status_code=500,
                retryable=True,
            ) from exc
        return result.stdout.strip()

    @staticmethod
    def _database_unavailable() -> ApiError:
        return ApiError(
            status_code=503,
            reason="DATABASE_UNAVAILABLE",
            detail="Snapshot 데이터베이스를 사용할 수 없습니다.",
            retryable=True,
        )
