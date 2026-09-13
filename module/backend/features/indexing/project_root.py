"""Resolve the immutable server-local project root used for VSS indexing."""

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
from backend.infrastructure.database.models import Snapshot, TrackedBranch

IndexRootSource = Literal["materialized_snapshot"]


@dataclass(frozen=True, slots=True)
class IndexProjectRoot:
    project_root: Path
    source: IndexRootSource


class IndexProjectRootResolver:
    """Use one verified immutable Snapshot tree for every VSS indexing request."""

    def __init__(self, *, materializer: SnapshotMaterializer) -> None:
        self._materializer = materializer

    async def lock_tracked_branch(
        self,
        session: AsyncSession,
        snapshot: Snapshot,
    ) -> TrackedBranch | None:
        """Lock and validate Branch metadata while a current-HEAD Index request is decided."""
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
        """Prevent overlapping submissions for one VSS project."""
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
                detail="같은 VSS project의 다른 Snapshot이 제출 또는 인덱싱 중입니다.",
                retryable=True,
                extra=self._snapshot_extra(snapshot),
            )

    async def verify_materialized(self, snapshot: Snapshot) -> IndexProjectRoot:
        """Verify the immutable exact revision before any VSS request."""
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
        _session: AsyncSession,
        snapshot: Snapshot,
        *,
        verified_materialized: IndexProjectRoot | None = None,
        locked_tracked_branch: TrackedBranch | None = None,
    ) -> IndexProjectRoot:
        """Return the verified immutable tree; mutable Branch worktrees are never exposed to VSS."""
        if locked_tracked_branch is not None:
            self._assert_tracked_branch_matches(snapshot, locked_tracked_branch)
        return verified_materialized or await self.verify_materialized(snapshot)

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
