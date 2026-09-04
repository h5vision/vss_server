"""추적 Branch, HEAD 관측과 저장소 sync lease 영속화."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.features.repository_collection.errors import CollectionError
from backend.features.repository_collection.schemas import (
    BranchChangeType,
    SyncTrigger,
    TrackedBranchCreateRequest,
)
from backend.infrastructure.database.models import (
    BranchHeadHistory,
    Repository,
    RepositorySyncRun,
    TrackedBranch,
)


class RepositoryCollectionStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_tracked_branch(
        self,
        request: TrackedBranchCreateRequest,
    ) -> TrackedBranch:
        repository = await self._session.get(Repository, request.repository_id)
        if repository is None:
            raise CollectionError(
                reason="REPOSITORY_NOT_FOUND",
                detail="추적 Branch를 연결할 Repository를 찾을 수 없습니다.",
                retryable=False,
                status_code=404,
            )
        if not repository.active:
            raise CollectionError(
                reason="REPOSITORY_INACTIVE",
                detail="비활성 Repository에는 추적 Branch를 추가할 수 없습니다.",
                retryable=False,
                status_code=409,
            )
        tracked_branch = TrackedBranch(**request.model_dump())
        self._session.add(tracked_branch)
        await self._session.flush()
        return tracked_branch

    async def get_tracked_branch(self, tracked_branch_id: UUID) -> TrackedBranch:
        tracked_branch = await self._session.get(TrackedBranch, tracked_branch_id)
        if tracked_branch is None:
            raise CollectionError(
                reason="TRACKED_BRANCH_NOT_FOUND",
                detail="요청한 추적 Branch를 찾을 수 없습니다.",
                retryable=False,
                status_code=404,
            )
        return tracked_branch

    async def list_tracked_branches(
        self,
        repository_id: UUID,
        *,
        tracked_only: bool = True,
    ) -> list[TrackedBranch]:
        statement = (
            select(TrackedBranch)
            .where(TrackedBranch.repository_id == repository_id)
            .order_by(TrackedBranch.branch_ref, TrackedBranch.tracked_branch_id)
        )
        if tracked_only:
            statement = statement.where(TrackedBranch.tracked.is_(True))
        return list(await self._session.scalars(statement))

    async def claim_sync(
        self,
        repository_id: UUID,
        *,
        request_id: UUID,
        trigger: SyncTrigger,
        lease_seconds: int,
    ) -> tuple[Repository, RepositorySyncRun]:
        now = datetime.now(timezone.utc)
        repository = await self._session.scalar(
            select(Repository)
            .where(Repository.repository_id == repository_id)
            .with_for_update()
        )
        if repository is None:
            raise CollectionError(
                reason="REPOSITORY_NOT_FOUND",
                detail="동기화할 Repository를 찾을 수 없습니다.",
                retryable=False,
                status_code=404,
            )
        if not repository.active:
            raise CollectionError(
                reason="REPOSITORY_INACTIVE",
                detail="비활성 Repository는 동기화할 수 없습니다.",
                retryable=False,
                status_code=409,
            )

        active = await self._session.scalar(
            select(RepositorySyncRun).where(
                RepositorySyncRun.repository_id == repository_id,
                RepositorySyncRun.state == "running",
            )
        )
        if active is not None:
            lease_expires_at = active.lease_expires_at
            if lease_expires_at.tzinfo is None:
                lease_expires_at = lease_expires_at.replace(tzinfo=timezone.utc)
            if lease_expires_at > now:
                raise CollectionError(
                    reason="COLLECTION_SYNC_ALREADY_RUNNING",
                    detail="같은 Repository의 Branch 동기화가 이미 진행 중입니다.",
                    retryable=True,
                    status_code=409,
                )
            active.state = "failed"
            active.reason = "COLLECTION_SYNC_LEASE_EXPIRED"
            active.detail = "이전 동기화 lease가 만료되어 실패로 종료하고 새 실행을 시작합니다."
            active.retryable = True
            active.finished_at = now

        sync_run = RepositorySyncRun(
            request_id=request_id,
            repository_id=repository_id,
            trigger=trigger,
            state="running",
            reason="COLLECTION_SYNC_RUNNING",
            detail="사용자가 선택한 Branch의 원격 HEAD를 확인하고 있습니다.",
            retryable=False,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            lease_generation=1,
        )
        self._session.add(sync_run)
        await self._session.flush()
        return repository, sync_run

    async def refresh_lease(
        self,
        sync_run: RepositorySyncRun,
        *,
        lease_seconds: int,
        expected_generation: int | None = None,
    ) -> int:
        if (
            expected_generation is not None
            and sync_run.lease_generation != expected_generation
        ):
            raise CollectionError(
                reason="COLLECTION_SYNC_FENCING_TOKEN_INVALID",
                detail=(
                    "Lease fencing token이 일치하지 않습니다. 다른 프로세스에 의해 lease가 "
                    "갱신되었을 수 있습니다."
                ),
                retryable=False,
                status_code=409,
            )
        sync_run.lease_generation += 1
        sync_run.lease_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=lease_seconds
        )
        await self._session.flush()
        return sync_run.lease_generation

    async def has_head_history(self, tracked_branch_id: UUID) -> bool:
        count = await self._session.scalar(
            select(func.count())
            .select_from(BranchHeadHistory)
            .where(BranchHeadHistory.tracked_branch_id == tracked_branch_id)
        )
        return bool(count)

    async def observe_head(
        self,
        tracked_branch: TrackedBranch,
        *,
        sync_run_id: UUID,
        previous_head_sha: str | None,
        observed_head_sha: str | None,
        change_type: BranchChangeType,
        observed_at: datetime,
    ) -> BranchHeadHistory:
        history = BranchHeadHistory(
            tracked_branch_id=tracked_branch.tracked_branch_id,
            sync_run_id=sync_run_id,
            previous_head_sha=previous_head_sha,
            observed_head_sha=observed_head_sha,
            change_type=change_type,
            observed_at=observed_at,
        )
        self._session.add(history)
        tracked_branch.current_head_sha = observed_head_sha
        tracked_branch.last_fetched_at = observed_at
        await self._session.flush()
        return history

    async def mark_unchanged(self, tracked_branch: TrackedBranch, *, observed_at: datetime) -> None:
        tracked_branch.last_fetched_at = observed_at
        await self._session.flush()

    async def finish_sync(
        self,
        sync_run: RepositorySyncRun,
        *,
        state: str,
        reason: str,
        detail: str,
        retryable: bool,
        result_json: list[dict],
        finished_at: datetime,
        expected_generation: int | None = None,
    ) -> None:
        if (
            expected_generation is not None
            and sync_run.lease_generation != expected_generation
        ):
            raise CollectionError(
                reason="COLLECTION_SYNC_FENCING_TOKEN_INVALID",
                detail="Lease fencing token이 일치하지 않아 동기화 결과를 반영하지 못했습니다.",
                retryable=False,
                status_code=409,
            )
        sync_run.state = state
        sync_run.reason = reason
        sync_run.detail = detail
        sync_run.retryable = retryable
        sync_run.result_json = result_json
        sync_run.finished_at = finished_at
        await self._session.flush()
