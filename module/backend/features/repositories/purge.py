"""Explicit destructive cleanup for one registered Repository.

Repository removal is deliberately separate from the existing deactivate operation.
The purge only deletes Snapshot/module metadata. VSS indexes are independent resources
and must be selected and deleted through the VSS maintenance boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.infrastructure.database.models import (
    BranchBinding,
    BranchHeadHistory,
    ChangeRequest,
    ChangeRequestRevision,
    CommitCatalogRun,
    Repository,
    RepositoryCommit,
    RepositoryCommitParent,
    RepositorySyncRun,
    RepositoryTag,
    Snapshot,
    SnapshotAttempt,
    SnapshotDelta,
    TagRevisionHistory,
    TrackedBranch,
)

_ACTIVE_SNAPSHOT_STATES = {"submitting", "accepted", "indexing"}


@dataclass(frozen=True, slots=True)
class RepositoryPurgeResult:
    repository_id: UUID
    canonical_name: str
    vss_project_ids: tuple[str, ...]
    deleted_rows: dict[str, int]


@dataclass(frozen=True, slots=True)
class RepositoryPurgeConflict(RuntimeError):
    reason: str
    detail: str
    retryable: bool = True

    def __str__(self) -> str:
        return self.detail


class RepositoryPurgeService:
    """Purge module-owned metadata after proving no module operation is in flight."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def purge(self, repository_id: UUID) -> RepositoryPurgeResult:
        repository = await self._session.get(Repository, repository_id)
        if repository is None:
            raise RepositoryPurgeConflict(
                reason="REPOSITORY_NOT_FOUND",
                detail="요청한 Repository를 찾을 수 없습니다.",
                retryable=False,
            )

        await self._assert_idle(repository_id)
        project_ids = await self._vss_project_ids(repository_id)

        tracked_ids = select(TrackedBranch.tracked_branch_id).where(
            TrackedBranch.repository_id == repository_id
        )
        sync_ids = select(RepositorySyncRun.sync_run_id).where(
            RepositorySyncRun.repository_id == repository_id
        )
        snapshot_ids = select(Snapshot.snapshot_id).where(Snapshot.repository_id == repository_id)
        change_request_ids = select(ChangeRequest.change_request_id).where(
            ChangeRequest.repository_id == repository_id
        )
        tag_ids = select(RepositoryTag.repository_tag_id).where(
            RepositoryTag.repository_id == repository_id
        )
        commit_ids = select(RepositoryCommit.repository_commit_id).where(
            RepositoryCommit.repository_id == repository_id
        )

        deleted: dict[str, int] = {}
        await self._delete(
            deleted,
            "snapshot_attempts",
            delete(SnapshotAttempt).where(SnapshotAttempt.snapshot_id.in_(snapshot_ids)),
        )
        await self._delete(
            deleted,
            "snapshot_deltas",
            delete(SnapshotDelta).where(SnapshotDelta.snapshot_id.in_(snapshot_ids)),
        )
        await self._delete(
            deleted,
            "change_request_revisions",
            delete(ChangeRequestRevision).where(
                ChangeRequestRevision.change_request_id.in_(change_request_ids)
            ),
        )
        await self._delete(
            deleted,
            "branch_head_history",
            delete(BranchHeadHistory).where(
                or_(
                    BranchHeadHistory.tracked_branch_id.in_(tracked_ids),
                    BranchHeadHistory.sync_run_id.in_(sync_ids),
                )
            ),
        )
        await self._delete(
            deleted,
            "tag_revision_history",
            delete(TagRevisionHistory).where(
                or_(
                    TagRevisionHistory.repository_tag_id.in_(tag_ids),
                    TagRevisionHistory.sync_run_id.in_(sync_ids),
                )
            ),
        )
        # Parent edges have two RESTRICT FKs into repository_commits. Remove either
        # side before deleting the commit catalog itself.
        await self._delete(
            deleted,
            "repository_commit_parents",
            delete(RepositoryCommitParent).where(
                or_(
                    RepositoryCommitParent.repository_commit_id.in_(commit_ids),
                    RepositoryCommitParent.parent_commit_id.in_(commit_ids),
                )
            ),
        )
        await self._delete(
            deleted,
            "snapshots",
            delete(Snapshot).where(Snapshot.repository_id == repository_id),
        )
        await self._delete(
            deleted,
            "branch_bindings",
            delete(BranchBinding).where(BranchBinding.repository_id == repository_id),
        )
        await self._delete(
            deleted,
            "tracked_branches",
            delete(TrackedBranch).where(TrackedBranch.repository_id == repository_id),
        )
        await self._delete(
            deleted,
            "repository_sync_runs",
            delete(RepositorySyncRun).where(RepositorySyncRun.repository_id == repository_id),
        )
        await self._delete(
            deleted,
            "change_requests",
            delete(ChangeRequest).where(ChangeRequest.repository_id == repository_id),
        )
        await self._delete(
            deleted,
            "repository_tags",
            delete(RepositoryTag).where(RepositoryTag.repository_id == repository_id),
        )
        await self._delete(
            deleted,
            "repository_commits",
            delete(RepositoryCommit).where(RepositoryCommit.repository_id == repository_id),
        )
        await self._delete(
            deleted,
            "commit_catalog_runs",
            delete(CommitCatalogRun).where(CommitCatalogRun.repository_id == repository_id),
        )
        await self._delete(
            deleted,
            "repositories",
            delete(Repository).where(Repository.repository_id == repository_id),
        )

        return RepositoryPurgeResult(
            repository_id=repository_id,
            canonical_name=repository.canonical_name,
            vss_project_ids=project_ids,
            deleted_rows=deleted,
        )

    async def _assert_idle(self, repository_id: UUID) -> None:
        running_sync = await self._session.scalar(
            select(RepositorySyncRun.sync_run_id)
            .where(
                RepositorySyncRun.repository_id == repository_id,
                RepositorySyncRun.state == "running",
            )
            .limit(1)
        )
        running_catalog = await self._session.scalar(
            select(CommitCatalogRun.run_id)
            .where(
                CommitCatalogRun.repository_id == repository_id,
                CommitCatalogRun.state == "running",
            )
            .limit(1)
        )
        active_snapshot = await self._session.scalar(
            select(Snapshot.snapshot_id)
            .where(
                Snapshot.repository_id == repository_id,
                Snapshot.state.in_(_ACTIVE_SNAPSHOT_STATES),
            )
            .limit(1)
        )
        if running_sync or running_catalog or active_snapshot:
            raise RepositoryPurgeConflict(
                reason="REPOSITORY_PURGE_BUSY",
                detail=(
                    "Repository에 실행 중인 sync, commit catalog 또는 VSS index 작업이 있어 "
                    "삭제할 수 없습니다. 작업 종료 후 다시 시도하세요."
                ),
                retryable=True,
            )

    async def _vss_project_ids(self, repository_id: UUID) -> tuple[str, ...]:
        values: set[str] = set()
        for statement in (
            select(TrackedBranch.vss_project_id).where(
                TrackedBranch.repository_id == repository_id
            ),
            select(BranchBinding.vss_project_id).where(
                BranchBinding.repository_id == repository_id
            ),
            select(Snapshot.vss_project_id).where(Snapshot.repository_id == repository_id),
        ):
            values.update(value for value in await self._session.scalars(statement) if value)
        return tuple(sorted(values))

    async def _delete(self, counts: dict[str, int], name: str, statement) -> None:
        result = await self._session.execute(statement)
        counts[name] = max(int(result.rowcount or 0), 0)
