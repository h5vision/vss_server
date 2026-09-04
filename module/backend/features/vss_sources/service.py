"""DB Snapshot과 immutable Git tree를 VSS용 검증 descriptor로 변환한다."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from backend.core.errors import ApiError
from backend.core.orchestration import MODULE_PUSH, IndexOrchestrationMode
from backend.features.materialization.errors import MaterializationError
from backend.features.materialization.service import SnapshotMaterializer
from backend.features.snapshots.store import SnapshotStore
from backend.features.vss_sources.schemas import (
    GitSourceVerification,
    VssChangeRequestDetailResponse,
    VssChangeRequestItem,
    VssChangeRequestListResponse,
    VssChangeRequestRevisionItem,
    VssCommitContext,
    VssContextResponse,
    VssContextSelection,
    VssPullCapabilitiesResponse,
    VssReferenceItem,
    VssReferenceListResponse,
    VssRevisionAvailability,
    VssRevisionItem,
    VssRevisionListResponse,
    VssSnapshotReadiness,
    VssSourceDescriptorResponse,
)
from backend.infrastructure.database.models import (
    BranchBinding,
    ChangeRequest,
    ChangeRequestRevision,
    Repository,
    RepositoryCommit,
    RepositoryCommitParent,
    RepositoryTag,
    Snapshot,
    TrackedBranch,
)
from backend.integrations.vss.schemas import VssIndexRequest


class VssSourceService:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        materializer: SnapshotMaterializer,
        git_timeout_seconds: float,
        index_orchestration_mode: IndexOrchestrationMode = MODULE_PUSH,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._materializer = materializer
        self._git_timeout_seconds = git_timeout_seconds
        self._index_orchestration_mode = index_orchestration_mode

    def capabilities(self, *, request_id: UUID) -> VssPullCapabilitiesResponse:
        module_starts_indexing = self._index_orchestration_mode == MODULE_PUSH
        return VssPullCapabilitiesResponse(
            detail="VSS가 사용할 수 있는 Snapshot pull 계약과 인덱싱 시작 소유권입니다.",
            request_id=request_id,
            orchestration_mode=self._index_orchestration_mode,
            index_start_owner="module" if module_starts_indexing else "vss",
            module_starts_indexing=module_starts_indexing,
            resources=["source", "revisions", "refs", "context", "change_requests"],
            context_selectors=["revision", "branch", "tag", "change_request"],
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
                tags = list(
                    await session.scalars(
                        select(RepositoryTag)
                        .where(
                            RepositoryTag.repository_id == repository.repository_id,
                            RepositoryTag.current_commit_sha.is_not(None),
                        )
                        .order_by(RepositoryTag.tag_ref)
                    )
                )
                revisions = {
                    branch.current_head_sha for branch in branches if branch.current_head_sha
                }
                revisions.update(
                    tag.current_commit_sha for tag in tags if tag.current_commit_sha
                )
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
        items.extend(
            VssReferenceItem(
                kind="tag",
                ref=tag.tag_ref,
                revision=tag.current_commit_sha,
                project_id=normalized_project_id,
                is_default=False,
                observed_at=tag.last_observed_at,
                readiness=self._snapshot_readiness(
                    snapshots_by_project_revision.get(
                        (normalized_project_id, tag.current_commit_sha)
                    )
                ),
            )
            for tag in tags
            if tag.current_commit_sha is not None
        )
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
        tag_ref: str | None,
        change_request_provider: str | None,
        change_request_number: int | None,
        change_request_role: str | None,
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
                    tag_ref=tag_ref,
                    change_request_provider=change_request_provider,
                    change_request_number=change_request_number,
                    change_request_role=change_request_role,
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
        tag_ref: str | None,
        change_request_provider: str | None,
        change_request_number: int | None,
        change_request_role: str | None,
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
        if tag_ref is not None:
            tag = await session.scalar(
                select(RepositoryTag).where(
                    RepositoryTag.repository_id == repository.repository_id,
                    RepositoryTag.tag_ref == tag_ref,
                )
            )
            if tag is None:
                raise self._context_ref_not_found("Tag")
            if tag.current_commit_sha is None:
                raise self._context_ref_unavailable("Tag")
            return tag.current_commit_sha, VssContextSelection(
                kind="tag",
                value=tag_ref,
                reason="TAG_TARGET",
            )

        if (
            change_request_provider is None
            or change_request_number is None
            or change_request_role is None
        ):
            raise ApiError(
                status_code=422,
                reason="VSS_CONTEXT_SELECTOR_INVALID",
                detail="완전한 Change Request selector가 필요합니다.",
                retryable=False,
            )
        change_request = await session.scalar(
            select(ChangeRequest).where(
                ChangeRequest.repository_id == repository.repository_id,
                ChangeRequest.provider == change_request_provider,
                ChangeRequest.external_number == change_request_number,
            )
        )
        if change_request is None:
            raise ApiError(
                status_code=404,
                reason="VSS_CHANGE_REQUEST_NOT_FOUND",
                detail="요청한 VSS project에서 PR/MR reference를 찾을 수 없습니다.",
                retryable=False,
            )
        revision_by_role = {
            "base": change_request.current_base_sha,
            "head": change_request.current_head_sha,
            "merge": change_request.current_merge_sha,
        }
        selected_revision = revision_by_role[change_request_role]
        if selected_revision is None:
            raise ApiError(
                status_code=409,
                reason="VSS_CONTEXT_REVISION_UNAVAILABLE",
                detail="선택한 PR/MR role에는 관측된 commit이 없습니다.",
                retryable=False,
            )
        return selected_revision, VssContextSelection(
            kind="change_request",
            value=f"{change_request_provider}:{change_request_number}",
            role=change_request_role,
            reason=f"CHANGE_REQUEST_{change_request_role.upper()}",
        )

    async def change_requests(
        self,
        project_id: str,
        *,
        state: str | None,
        limit: int,
        request_id: UUID,
    ) -> VssChangeRequestListResponse:
        normalized_project_id = project_id.strip()
        async with self._sessionmaker() as session:
            try:
                repository = await self._repository_for_project(session, normalized_project_id)
                statement = (
                    select(ChangeRequest)
                    .where(ChangeRequest.repository_id == repository.repository_id)
                    .order_by(ChangeRequest.last_observed_at.desc())
                    .limit(limit)
                )
                if state is not None:
                    statement = statement.where(ChangeRequest.state == state)
                change_requests = list(await session.scalars(statement))
                items = [
                    await self._change_request_item(
                        session,
                        item,
                        project_id=normalized_project_id,
                    )
                    for item in change_requests
                ]
            except ApiError:
                raise
            except SQLAlchemyError as exc:
                raise self._database_unavailable() from exc
        return VssChangeRequestListResponse(
            detail="VSS project의 Repository에 연결된 PR/MR revision context입니다.",
            request_id=request_id,
            project_id=normalized_project_id,
            items=items,
        )

    async def change_request(
        self,
        project_id: str,
        *,
        provider: str,
        external_number: int,
        request_id: UUID,
    ) -> VssChangeRequestDetailResponse:
        normalized_project_id = project_id.strip()
        async with self._sessionmaker() as session:
            try:
                repository = await self._repository_for_project(session, normalized_project_id)
                change_request = await session.scalar(
                    select(ChangeRequest).where(
                        ChangeRequest.repository_id == repository.repository_id,
                        ChangeRequest.provider == provider,
                        ChangeRequest.external_number == external_number,
                    )
                )
                if change_request is None:
                    raise ApiError(
                        status_code=404,
                        reason="VSS_CHANGE_REQUEST_NOT_FOUND",
                        detail="요청한 VSS project에서 PR/MR reference를 찾을 수 없습니다.",
                        retryable=False,
                    )
                item = await self._change_request_item(
                    session,
                    change_request,
                    project_id=normalized_project_id,
                )
                observations = list(
                    await session.scalars(
                        select(ChangeRequestRevision)
                        .where(
                            ChangeRequestRevision.change_request_id
                            == change_request.change_request_id
                        )
                        .order_by(ChangeRequestRevision.observed_at)
                    )
                )
            except ApiError:
                raise
            except SQLAlchemyError as exc:
                raise self._database_unavailable() from exc
        return VssChangeRequestDetailResponse(
            **item.model_dump(),
            detail="PR/MR의 현재 revision과 append-only 관측 이력입니다.",
            request_id=request_id,
            project_id=normalized_project_id,
            observations=[
                VssChangeRequestRevisionItem.model_validate(value, from_attributes=True)
                for value in observations
            ],
        )

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

    async def _change_request_item(
        self,
        session,
        change_request: ChangeRequest,
        *,
        project_id: str,
    ) -> VssChangeRequestItem:
        revision_roles = [
            ("base", change_request.current_base_sha),
            ("head", change_request.current_head_sha),
        ]
        if change_request.current_merge_sha is not None:
            revision_roles.append(("merge", change_request.current_merge_sha))
        snapshots = list(
            await session.scalars(
                select(Snapshot)
                .where(
                    Snapshot.repository_id == change_request.repository_id,
                    Snapshot.vss_project_id == project_id,
                    Snapshot.target_revision.in_([revision for _, revision in revision_roles]),
                )
                .order_by(Snapshot.updated_at.desc())
            )
        )
        snapshot_by_revision = {}
        for snapshot in snapshots:
            snapshot_by_revision.setdefault(snapshot.target_revision, snapshot)
        return VssChangeRequestItem(
            change_request_id=change_request.change_request_id,
            repository_id=change_request.repository_id,
            provider=change_request.provider,
            external_number=change_request.external_number,
            kind=change_request.kind,
            state=change_request.state,
            title=change_request.title,
            base_ref=change_request.base_ref,
            head_ref=change_request.head_ref,
            base_sha=change_request.current_base_sha,
            head_sha=change_request.current_head_sha,
            merge_sha=change_request.current_merge_sha,
            last_observed_at=change_request.last_observed_at,
            provider_updated_at=change_request.provider_updated_at,
            merged_at=change_request.merged_at,
            revisions=[
                self._revision_availability(role, revision, snapshot_by_revision.get(revision))
                for role, revision in revision_roles
            ],
        )

    @staticmethod
    def _revision_availability(
        role: str,
        revision: str,
        snapshot: Snapshot | None,
    ) -> VssRevisionAvailability:
        if snapshot is None:
            return VssRevisionAvailability(
                role=role,
                revision=revision,
                eligible_for_answer=False,
                unavailable_reason="SNAPSHOT_NOT_FOUND",
            )
        eligible = snapshot.state == "completed" and snapshot.vss_state == "done"
        return VssRevisionAvailability(
            role=role,
            revision=revision,
            snapshot_id=snapshot.snapshot_id,
            snapshot_state=snapshot.state,
            vss_state=snapshot.vss_state,
            eligible_for_answer=eligible,
            unavailable_reason=None if eligible else "SNAPSHOT_NOT_COMPLETED",
        )

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
