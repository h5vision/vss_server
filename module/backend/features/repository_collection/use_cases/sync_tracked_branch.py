"""Use case for synchronizing a single tracked branch and connecting it to snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from backend.features.repository_collection.publisher import CollectedSnapshotPublisher
from backend.features.repository_collection.schemas import (
    BranchChangeType,
    BranchSyncOutcome,
)
from backend.features.repository_collection.store import RepositoryCollectionStore
from backend.features.snapshots.store import SnapshotStore
from backend.infrastructure.database.models import Repository, Snapshot, TrackedBranch
from backend.ports.git import CommitGraphReader, RemoteObjectFetcher


@dataclass(frozen=True, slots=True)
class SyncTrackedBranchUseCase:
    """Synchronizes a single tracked branch, fetches objects, and promotes snapshots."""

    sessionmaker: async_sessionmaker[AsyncSession]
    object_fetcher: RemoteObjectFetcher
    graph_reader: CommitGraphReader
    publisher: CollectedSnapshotPublisher

    async def sync_branch(
        self,
        repository: Repository,
        *,
        tracked_branch_id: UUID,
        sync_run_id: UUID,
        lease_generation: int,
        request_id: UUID,
        remote_head: str | None,
    ) -> BranchSyncOutcome:
        async with self.sessionmaker() as session:
            store = RepositoryCollectionStore(session)
            await store.assert_sync_owner(
                sync_run_id,
                expected_generation=lease_generation,
            )
            tracked_branch = await store.get_tracked_branch(tracked_branch_id)
            previous_head = tracked_branch.current_head_sha
            branch_ref = tracked_branch.branch_ref
            if not tracked_branch.tracked:
                return BranchSyncOutcome(
                    ok=True,
                    reason="TRACKED_BRANCH_DISABLED",
                    detail="추적이 비활성화된 Branch는 동기화하지 않았습니다.",
                    retryable=False,
                    tracked_branch_id=tracked_branch_id,
                    branch_ref=branch_ref,
                    previous_head_sha=previous_head,
                    observed_head_sha=previous_head,
                )
            if remote_head is None:
                return await self._observe_deleted_branch(
                    session,
                    store,
                    tracked_branch,
                    sync_run_id=sync_run_id,
                )

        observed_head = await run_in_threadpool(
            self.object_fetcher.fetch_branch,
            repository_id=repository.repository_id,
            tracked_branch_id=tracked_branch_id,
            remote_url=repository.remote_url,
            branch_ref=branch_ref,
        )
        observed_at = datetime.now(timezone.utc)

        async with self.sessionmaker() as session:
            store = RepositoryCollectionStore(session)
            tracked_branch = await store.get_tracked_branch(tracked_branch_id)
            previous_head = tracked_branch.current_head_sha
            if previous_head == observed_head:
                await store.assert_sync_owner(
                    sync_run_id,
                    expected_generation=lease_generation,
                )
                snapshot_store = SnapshotStore(session)
                existing = await snapshot_store.find_by_target(
                    tracked_branch.vss_project_id,
                    observed_head,
                )
                snapshot = existing
                if snapshot is None:
                    snapshot = await snapshot_store.create_from_collection(
                        request_id=request_id,
                        tracked_branch=tracked_branch,
                        base_revision=observed_head,
                        target_revision=observed_head,
                    )
                await store.mark_unchanged(tracked_branch, observed_at=observed_at)
                await session.commit()
                if snapshot.state in {"validated", "materializing", "materialized"}:
                    published = await self.publisher.publish(
                        snapshot.snapshot_id,
                        request_id=request_id,
                        sync_run_id=sync_run_id,
                        lease_generation=lease_generation,
                    )
                    return BranchSyncOutcome(
                        ok=published.ok,
                        reason=published.reason,
                        detail=published.detail,
                        retryable=published.retryable,
                        tracked_branch_id=tracked_branch_id,
                        branch_ref=branch_ref,
                        previous_head_sha=previous_head,
                        observed_head_sha=observed_head,
                        snapshot_id=published.snapshot_id,
                        snapshot_state=published.snapshot_state,
                    )
                if existing is not None and existing.state in {
                    "failed",
                    "rejected",
                    "aborted",
                }:
                    return BranchSyncOutcome(
                        ok=False,
                        reason="SNAPSHOT_ALREADY_EXISTS",
                        detail=(
                            "같은 HEAD의 실패한 Snapshot이 있어 자동 재제출하지 않았습니다. "
                            "인증된 수동 재시도가 필요합니다."
                        ),
                        retryable=True,
                        tracked_branch_id=tracked_branch_id,
                        branch_ref=branch_ref,
                        previous_head_sha=previous_head,
                        observed_head_sha=observed_head,
                        snapshot_id=existing.snapshot_id,
                        snapshot_state=existing.state,
                    )
                return BranchSyncOutcome(
                    ok=True,
                    reason="BRANCH_HEAD_UNCHANGED",
                    detail=(
                        "선택한 Branch의 HEAD가 변경되지 않아 Snapshot을 다시 만들지 않았습니다."
                    ),
                    retryable=False,
                    tracked_branch_id=tracked_branch_id,
                    branch_ref=branch_ref,
                    previous_head_sha=previous_head,
                    observed_head_sha=observed_head,
                    snapshot_id=snapshot.snapshot_id,
                    snapshot_state=snapshot.state,
                )

            had_history = await store.has_head_history(tracked_branch_id)
            change_type = await self._classify_change(
                repository.repository_id,
                previous_head=previous_head,
                observed_head=observed_head,
                had_history=had_history,
            )
            base_revision = previous_head or observed_head
            await store.assert_sync_owner(
                sync_run_id,
                expected_generation=lease_generation,
            )
            snapshot_store = SnapshotStore(session)
            existing = await snapshot_store.find_by_target(
                tracked_branch.vss_project_id,
                observed_head,
            )
            if existing is not None and existing.state in {
                "completed",
                "already_indexed",
                "failed",
                "rejected",
                "aborted",
            }:
                await store.observe_head(
                    tracked_branch,
                    sync_run_id=sync_run_id,
                    previous_head_sha=previous_head,
                    observed_head_sha=observed_head,
                    change_type=change_type,
                    observed_at=observed_at,
                )
                await session.commit()
                return self._existing_snapshot_outcome(
                    tracked_branch_id=tracked_branch_id,
                    branch_ref=branch_ref,
                    previous_head=previous_head,
                    observed_head=observed_head,
                    change_type=change_type,
                    snapshot=existing,
                )

            snapshot = existing
            if snapshot is None:
                snapshot = await snapshot_store.create_from_collection(
                    request_id=request_id,
                    tracked_branch=tracked_branch,
                    base_revision=base_revision,
                    target_revision=observed_head,
                )
            await store.observe_head(
                tracked_branch,
                sync_run_id=sync_run_id,
                previous_head_sha=previous_head,
                observed_head_sha=observed_head,
                change_type=change_type,
                observed_at=observed_at,
            )
            await session.commit()

        published = await self.publisher.publish(
            snapshot.snapshot_id,
            request_id=request_id,
            sync_run_id=sync_run_id,
            lease_generation=lease_generation,
        )
        return BranchSyncOutcome(
            ok=published.ok,
            reason=published.reason,
            detail=published.detail,
            retryable=published.retryable,
            tracked_branch_id=tracked_branch_id,
            branch_ref=branch_ref,
            previous_head_sha=previous_head,
            observed_head_sha=observed_head,
            change_type=change_type,
            snapshot_id=published.snapshot_id,
            snapshot_state=published.snapshot_state,
        )

    async def _classify_change(
        self,
        repository_id: UUID,
        *,
        previous_head: str | None,
        observed_head: str,
        had_history: bool,
    ) -> BranchChangeType:
        if previous_head is None:
            return "recreated" if had_history else "created"
        is_ancestor = await run_in_threadpool(
            self.graph_reader.is_ancestor,
            repository_id,
            previous_head,
            observed_head,
        )
        return "fast_forward" if is_ancestor else "rewind"

    async def _observe_deleted_branch(
        self,
        session: AsyncSession,
        store: RepositoryCollectionStore,
        tracked_branch: TrackedBranch,
        *,
        sync_run_id: UUID,
    ) -> BranchSyncOutcome:
        previous_head = tracked_branch.current_head_sha
        if previous_head is None:
            return BranchSyncOutcome(
                ok=True,
                reason="BRANCH_ALREADY_DELETED",
                detail="원격에서 삭제된 상태가 유지되고 있습니다.",
                retryable=False,
                tracked_branch_id=tracked_branch.tracked_branch_id,
                branch_ref=tracked_branch.branch_ref,
                previous_head_sha=None,
                observed_head_sha=None,
                change_type="deleted",
            )
        observed_at = datetime.now(timezone.utc)
        await store.observe_head(
            tracked_branch,
            sync_run_id=sync_run_id,
            previous_head_sha=previous_head,
            observed_head_sha=None,
            change_type="deleted",
            observed_at=observed_at,
        )
        await session.commit()
        return BranchSyncOutcome(
            ok=True,
            reason="BRANCH_DELETED",
            detail="원격 Branch 삭제를 기록했으며 과거 HEAD 이력과 Git object는 보존합니다.",
            retryable=False,
            tracked_branch_id=tracked_branch.tracked_branch_id,
            branch_ref=tracked_branch.branch_ref,
            previous_head_sha=previous_head,
            observed_head_sha=None,
            change_type="deleted",
        )

    @staticmethod
    def _existing_snapshot_outcome(
        *,
        tracked_branch_id: UUID,
        branch_ref: str,
        previous_head: str | None,
        observed_head: str,
        change_type: BranchChangeType,
        snapshot: Snapshot,
    ) -> BranchSyncOutcome:
        completed = snapshot.state in {"completed", "already_indexed"}
        failed = snapshot.state in {"failed", "rejected", "aborted"}
        return BranchSyncOutcome(
            ok=not failed,
            reason="TARGET_ALREADY_INDEXED" if completed else "SNAPSHOT_ALREADY_EXISTS",
            detail=(
                "같은 target revision이 이미 VSS index로 완료되어 중복 제출하지 않았습니다."
                if completed
                else "같은 target revision의 Snapshot이 이미 있어 중복 제출하지 않았습니다."
            ),
            retryable=failed,
            tracked_branch_id=tracked_branch_id,
            branch_ref=branch_ref,
            previous_head_sha=previous_head,
            observed_head_sha=observed_head,
            change_type=change_type,
            snapshot_id=snapshot.snapshot_id,
            snapshot_state=snapshot.state,
        )
