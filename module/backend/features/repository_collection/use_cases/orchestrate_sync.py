"""Use case for orchestrating repository sync runs with lease management and error boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from backend.features.change_requests.errors import ChangeRequestError
from backend.features.commit_catalog.errors import CommitCatalogError
from backend.features.repository_collection.errors import CollectionError
from backend.features.repository_collection.schemas import (
    BranchSyncOutcome,
    RepositorySyncResult,
    SyncTrigger,
)
from backend.features.repository_collection.store import RepositoryCollectionStore
from backend.features.repository_collection.use_cases.sync_tracked_branch import (
    SyncTrackedBranchUseCase,
)
from backend.infrastructure.database.models import Repository, RepositorySyncRun
from backend.ports.git import RemoteRefReader

if TYPE_CHECKING:
    from backend.features.change_requests.service import ChangeRequestCollectionService
    from backend.features.commit_catalog.service import CommitCatalogService
    from backend.features.repository_tags.service import RepositoryTagService


@dataclass(frozen=True, slots=True)
class SyncRepositoryUseCase:
    """Orchestrates end-to-end repository sync, managing distributed leases and child syncs."""

    sessionmaker: async_sessionmaker[AsyncSession]
    ref_reader: RemoteRefReader
    sync_branch_use_case: SyncTrackedBranchUseCase
    sync_lease_seconds: int = 300
    commit_catalog_service: CommitCatalogService | None = None
    change_request_service: ChangeRequestCollectionService | None = None
    tag_service: RepositoryTagService | None = None

    async def sync_repository(
        self,
        repository_id: UUID,
        *,
        trigger: SyncTrigger = "manual",
        request_id: UUID | None = None,
    ) -> RepositorySyncResult:
        if trigger not in {"manual", "periodic"}:
            raise CollectionError(
                reason="COLLECTION_TRIGGER_UNSUPPORTED",
                detail="현재 수집 코어는 manual 또는 periodic trigger만 허용합니다.",
                retryable=False,
                status_code=422,
            )
        resolved_request_id = request_id or uuid4()
        repository, sync_run = await self._claim_sync(
            repository_id,
            request_id=resolved_request_id,
            trigger=trigger,
        )
        outcomes: list[BranchSyncOutcome] = []
        try:
            remote_heads = await run_in_threadpool(
                self.ref_reader.list_remote_heads,
                repository.remote_url,
            )
            heads_by_ref = {item.branch_ref: item.commit_sha for item in remote_heads}
            tracked_branch_ids = await self._tracked_branch_ids(repository_id)
            for tracked_branch_id in tracked_branch_ids:
                try:
                    outcome = await self.sync_branch_use_case.sync_branch(
                        repository,
                        tracked_branch_id=tracked_branch_id,
                        sync_run_id=sync_run.sync_run_id,
                        request_id=resolved_request_id,
                        remote_head=heads_by_ref.get(
                            await self._branch_ref(tracked_branch_id)
                        ),
                    )
                except CollectionError as exc:
                    branch_ref = await self._branch_ref(tracked_branch_id)
                    outcome = BranchSyncOutcome(
                        ok=False,
                        reason=exc.reason,
                        detail=exc.detail,
                        retryable=exc.retryable,
                        tracked_branch_id=tracked_branch_id,
                        branch_ref=branch_ref,
                    )
                outcomes.append(outcome)
                await self._refresh_lease(sync_run.sync_run_id)
        except CollectionError as exc:
            return await self._finish_failed_run(sync_run, exc, outcomes=outcomes)
        except SQLAlchemyError:
            return await self._finish_failed_run(
                sync_run,
                self._database_failure(),
                outcomes=outcomes,
            )

        if not outcomes:
            return await self._finish_run(
                sync_run,
                outcomes=outcomes,
                ok=True,
                reason="COLLECTION_NO_TRACKED_BRANCHES",
                detail="사용자가 선택한 추적 Branch가 없어 Repository를 변경하지 않았습니다.",
                retryable=False,
            )

        tag_failure = None
        if self.tag_service is not None:
            try:
                await self.tag_service.sync_repository(
                    repository_id,
                    sync_run_id=sync_run.sync_run_id,
                    progress=lambda: self._refresh_lease(sync_run.sync_run_id),
                )
            except CollectionError as exc:
                tag_failure = exc

        change_request_failure = None
        if (
            self.change_request_service is not None
            and self.change_request_service.supports(repository.provider)
        ):
            try:
                await self.change_request_service.sync_repository(
                    repository_id,
                    progress=lambda: self._refresh_lease(sync_run.sync_run_id),
                )
            except ChangeRequestError as exc:
                change_request_failure = exc

        catalog_failure = None
        if self.commit_catalog_service is not None:
            try:
                await self.commit_catalog_service.catalog_repository(
                    repository_id,
                    request_id=resolved_request_id,
                )
            except CommitCatalogError as exc:
                catalog_failure = exc

        failures = [item for item in outcomes if not item.ok]
        if failures:
            if len(failures) == 1:
                failure = failures[0]
                return await self._finish_run(
                    sync_run,
                    outcomes=outcomes,
                    ok=False,
                    reason=failure.reason,
                    detail=failure.detail,
                    retryable=failure.retryable,
                )
            return await self._finish_run(
                sync_run,
                outcomes=outcomes,
                ok=False,
                reason="COLLECTION_SYNC_PARTIAL_FAILURE",
                detail="일부 추적 Branch를 수집하거나 VSS에 제출하지 못했습니다.",
                retryable=any(item.retryable for item in failures),
            )

        if change_request_failure is not None:
            return await self._finish_run(
                sync_run,
                outcomes=outcomes,
                ok=False,
                reason=change_request_failure.reason,
                detail=(
                    "Branch Snapshot 처리는 완료됐지만 PR/MR 수집에 실패했습니다. "
                    f"{change_request_failure.detail}"
                ),
                retryable=change_request_failure.retryable,
            )

        if tag_failure is not None:
            return await self._finish_run(
                sync_run,
                outcomes=outcomes,
                ok=False,
                reason=tag_failure.reason,
                detail=(
                    "Branch Snapshot 처리는 완료됐지만 Tag 수집에 실패했습니다. "
                    f"{tag_failure.detail}"
                ),
                retryable=tag_failure.retryable,
            )

        if catalog_failure is not None:
            return await self._finish_run(
                sync_run,
                outcomes=outcomes,
                ok=False,
                reason=catalog_failure.reason,
                detail=(
                    "Branch Snapshot 처리는 완료됐지만 Commit catalog 갱신에 실패했습니다. "
                    f"{catalog_failure.detail}"
                ),
                retryable=catalog_failure.retryable,
            )

        return await self._finish_run(
            sync_run,
            outcomes=outcomes,
            ok=True,
            reason="COLLECTION_SYNC_COMPLETED",
            detail="선택한 Branch의 HEAD 관측과 필요한 Snapshot 제출을 완료했습니다.",
            retryable=False,
        )

    async def _claim_sync(
        self,
        repository_id: UUID,
        *,
        request_id: UUID,
        trigger: SyncTrigger,
    ) -> tuple[Repository, RepositorySyncRun]:
        async with self.sessionmaker() as session:
            try:
                repository, sync_run = await RepositoryCollectionStore(session).claim_sync(
                    repository_id,
                    request_id=request_id,
                    trigger=trigger,
                    lease_seconds=self.sync_lease_seconds,
                )
                await session.commit()
                return repository, sync_run
            except CollectionError:
                await session.rollback()
                raise
            except IntegrityError as exc:
                await session.rollback()
                raise CollectionError(
                    reason="COLLECTION_SYNC_ALREADY_RUNNING",
                    detail="같은 Repository의 Branch 동기화가 이미 진행 중입니다.",
                    retryable=True,
                    status_code=409,
                ) from exc
            except SQLAlchemyError as exc:
                await session.rollback()
                raise self._database_failure() from exc

    async def _refresh_lease(self, sync_run_id: UUID) -> None:
        async with self.sessionmaker() as session:
            try:
                sync_run = await session.get(RepositorySyncRun, sync_run_id)
                if sync_run is None:
                    raise self._database_failure()
                await RepositoryCollectionStore(session).refresh_lease(
                    sync_run,
                    lease_seconds=self.sync_lease_seconds,
                )
                await session.commit()
            except SQLAlchemyError as exc:
                await session.rollback()
                raise self._database_failure() from exc

    async def _tracked_branch_ids(self, repository_id: UUID) -> list[UUID]:
        async with self.sessionmaker() as session:
            try:
                branches = await RepositoryCollectionStore(session).list_tracked_branches(
                    repository_id
                )
                return [item.tracked_branch_id for item in branches]
            except SQLAlchemyError as exc:
                raise self._database_failure() from exc

    async def _branch_ref(self, tracked_branch_id: UUID) -> str:
        async with self.sessionmaker() as session:
            try:
                branch = await RepositoryCollectionStore(session).get_tracked_branch(
                    tracked_branch_id
                )
                return branch.branch_ref
            except SQLAlchemyError as exc:
                raise self._database_failure() from exc

    async def _finish_failed_run(
        self,
        sync_run: RepositorySyncRun,
        error: CollectionError,
        *,
        outcomes: list[BranchSyncOutcome],
    ) -> RepositorySyncResult:
        return await self._finish_run(
            sync_run,
            outcomes=outcomes,
            ok=False,
            reason=error.reason,
            detail=error.detail,
            retryable=error.retryable,
        )

    async def _finish_run(
        self,
        sync_run: RepositorySyncRun,
        *,
        outcomes: list[BranchSyncOutcome],
        ok: bool,
        reason: str,
        detail: str,
        retryable: bool,
    ) -> RepositorySyncResult:
        finished_at = datetime.now(timezone.utc)
        state = "succeeded" if ok else "failed"
        async with self.sessionmaker() as session:
            try:
                persisted = await session.get(RepositorySyncRun, sync_run.sync_run_id)
                if persisted is None:
                    raise self._database_failure()
                await RepositoryCollectionStore(session).finish_sync(
                    persisted,
                    state=state,
                    reason=reason,
                    detail=detail,
                    retryable=retryable,
                    result_json=[
                        item.model_dump(mode="json", exclude_none=True)
                        for item in outcomes
                    ],
                    finished_at=finished_at,
                )
                await session.commit()
                started_at = persisted.started_at
            except CollectionError:
                await session.rollback()
                raise
            except SQLAlchemyError as exc:
                await session.rollback()
                raise self._database_failure() from exc
        return RepositorySyncResult(
            ok=ok,
            reason=reason,
            detail=detail,
            retryable=retryable,
            sync_run_id=sync_run.sync_run_id,
            repository_id=sync_run.repository_id,
            trigger=sync_run.trigger,
            state=state,
            started_at=started_at,
            finished_at=finished_at,
            outcomes=outcomes,
        )

    @staticmethod
    def _database_failure() -> CollectionError:
        return CollectionError(
            reason="DATABASE_UNAVAILABLE",
            detail="Repository 수집용 Snapshot 데이터베이스를 사용할 수 없습니다.",
            retryable=True,
            status_code=503,
        )
