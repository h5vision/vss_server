"""Resolve the server-local project root used for one VSS indexing request."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from backend.core.errors import ApiError
from backend.features.materialization.errors import MaterializationError
from backend.features.materialization.service import SnapshotMaterializer
from backend.features.repository_collection.errors import CollectionError
from backend.infrastructure.database.models import Repository, Snapshot, TrackedBranch
from backend.ports.git import ManagedRepositoryWorkspace

IndexRootSource = Literal["managed_branch", "materialized_snapshot"]


@dataclass(frozen=True, slots=True)
class IndexProjectRoot:
    project_root: Path
    source: IndexRootSource


class IndexProjectRootResolver:
    """Verify immutable proof first, then optionally expose an exact current Branch workspace."""

    def __init__(
        self,
        *,
        materializer: SnapshotMaterializer,
        workspace_manager: ManagedRepositoryWorkspace | None,
    ) -> None:
        self._materializer = materializer
        self._workspace_manager = workspace_manager

    async def lock_tracked_branch(
        self,
        session: AsyncSession,
        snapshot: Snapshot,
    ) -> TrackedBranch | None:
        """Serialize all Index/Retry operations that may share one Branch working copy."""
        if snapshot.tracked_branch_id is None:
            return None
        try:
            tracked_branch = await session.scalar(
                select(TrackedBranch)
                .where(TrackedBranch.tracked_branch_id == snapshot.tracked_branch_id)
                .with_for_update()
            )
        except SQLAlchemyError as exc:
            raise self._database_unavailable(snapshot) from exc
        if tracked_branch is None:
            raise self._source_inconsistent(
                snapshot,
                "Snapshot의 Tracked Branch 원본 정보를 찾을 수 없습니다.",
            )
        self._assert_tracked_branch_matches(snapshot, tracked_branch)
        return tracked_branch

    async def assert_no_other_active_snapshot(
        self,
        session: AsyncSession,
        snapshot: Snapshot,
    ) -> None:
        """Close DB/VSS observation gaps before a shared project_root can be mutated."""
        try:
            active_snapshot_id = await session.scalar(
                select(Snapshot.snapshot_id)
                .where(
                    Snapshot.vss_project_id == snapshot.vss_project_id,
                    Snapshot.snapshot_id != snapshot.snapshot_id,
                    Snapshot.state.in_(("submitting", "accepted", "indexing")),
                )
                .limit(1)
            )
        except SQLAlchemyError as exc:
            raise self._database_unavailable(snapshot) from exc
        if active_snapshot_id is not None:
            raise ApiError(
                status_code=409,
                reason="VSS_INDEX_ALREADY_RUNNING",
                detail=(
                    "같은 VSS project의 다른 Snapshot이 제출 또는 인덱싱 중이어서 "
                    "working copy를 변경하지 않았습니다."
                ),
                retryable=True,
                extra=self._snapshot_extra(snapshot),
            )

    async def verify_materialized(self, snapshot: Snapshot) -> IndexProjectRoot:
        """Verify the immutable Snapshot before any VSS request or mutable workspace refresh."""
        if snapshot.materialized_locator is None:
            raise ApiError(
                status_code=409,
                reason="SNAPSHOT_INDEX_MATERIALIZATION_REQUIRED",
                detail="검증된 immutable revision이 없어 VSS 인덱싱을 시작할 수 없습니다.",
                retryable=False,
                extra=self._snapshot_extra(snapshot),
            )
        try:
            materialized = await run_in_threadpool(
                self._materializer.verify_existing,
                snapshot.materialized_locator,
                snapshot.target_revision,
            )
        except MaterializationError as exc:
            raise ApiError(
                status_code=exc.status_code,
                reason=exc.reason,
                detail=exc.detail,
                retryable=exc.retryable,
                extra=self._snapshot_extra(snapshot),
            ) from exc
        return IndexProjectRoot(
            project_root=materialized.project_root,
            source="materialized_snapshot",
        )

    async def resolve(
        self,
        session: AsyncSession,
        snapshot: Snapshot,
        *,
        verified_materialized: IndexProjectRoot | None = None,
        locked_tracked_branch: TrackedBranch | None = None,
    ) -> IndexProjectRoot:
        materialized = verified_materialized or await self.verify_materialized(snapshot)
        if self._workspace_manager is None or snapshot.tracked_branch_id is None:
            return materialized

        tracked_branch = locked_tracked_branch or await self.lock_tracked_branch(session, snapshot)
        try:
            repository = await session.get(Repository, snapshot.repository_id)
        except SQLAlchemyError as exc:
            raise self._database_unavailable(snapshot) from exc
        if repository is None:
            raise self._source_inconsistent(
                snapshot,
                "Snapshot의 Repository 원본 정보를 찾을 수 없습니다.",
            )
        self._assert_tracked_branch_matches(snapshot, tracked_branch)

        # Historical Snapshots remain indexable from their immutable tree. Only the exact current
        # tracked HEAD is exposed through the mutable branch workspace under /home/ubuntu/repos.
        if (
            not tracked_branch.tracked
            or tracked_branch.current_head_sha != snapshot.target_revision
        ):
            return materialized

        try:
            workspace = await run_in_threadpool(
                self._workspace_manager.ensure_branch,
                repository_id=repository.repository_id,
                canonical_name=repository.canonical_name,
                remote_url=repository.remote_url,
                branch_ref=tracked_branch.branch_ref,
                expected_revision=snapshot.target_revision,
            )
        except CollectionError as exc:
            raise ApiError(
                status_code=exc.status_code,
                reason=exc.reason,
                detail=exc.detail,
                retryable=exc.retryable,
                extra=self._snapshot_extra(snapshot),
            ) from exc

        return IndexProjectRoot(project_root=workspace, source="managed_branch")

    @classmethod
    def _database_unavailable(cls, snapshot: Snapshot) -> ApiError:
        return ApiError(
            status_code=503,
            reason="ADMIN_DATABASE_UNAVAILABLE",
            detail="VSS 인덱싱용 Repository/Branch 정보를 읽지 못했습니다.",
            retryable=True,
            extra=cls._snapshot_extra(snapshot),
        )

    @classmethod
    def _source_inconsistent(cls, snapshot: Snapshot, detail: str) -> ApiError:
        return ApiError(
            status_code=500,
            reason="SNAPSHOT_SOURCE_INCONSISTENT",
            detail=detail,
            retryable=False,
            extra=cls._snapshot_extra(snapshot),
        )

    @classmethod
    def _assert_tracked_branch_matches(
        cls,
        snapshot: Snapshot,
        tracked_branch: TrackedBranch,
    ) -> None:
        if (
            tracked_branch.repository_id != snapshot.repository_id
            or tracked_branch.branch_ref != snapshot.branch_ref
            or tracked_branch.vss_project_id != snapshot.vss_project_id
        ):
            raise cls._source_inconsistent(
                snapshot,
                "Snapshot과 Tracked Branch의 Repository/Branch/VSS 연결이 일치하지 않습니다.",
            )

    @staticmethod
    def _snapshot_extra(snapshot: Snapshot) -> dict[str, str]:
        return {
            "snapshot_id": str(snapshot.snapshot_id),
            "target_revision": snapshot.target_revision,
            "state": snapshot.state,
        }
