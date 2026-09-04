"""사용자가 선택한 Branch만 수집하고 새 HEAD를 Snapshot/VSS 흐름에 연결한다."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.features.repository_collection.errors import CollectionError
from backend.features.repository_collection.git_client import RepositoryGitClient
from backend.features.repository_collection.publisher import CollectedSnapshotPublisher
from backend.features.repository_collection.schemas import (
    BranchChangeType,
    BranchSyncOutcome,
    RepositoryCatalogResult,
    RepositorySyncResult,
    SyncTrigger,
    TrackedBranchCreateRequest,
    TrackedBranchResponse,
)
from backend.features.repository_collection.store import RepositoryCollectionStore
from backend.features.repository_collection.use_cases.observe_repository import (
    ObserveRepositoryUseCase,
)
from backend.features.repository_collection.use_cases.orchestrate_sync import (
    SyncRepositoryUseCase,
)
from backend.features.repository_collection.use_cases.sync_tracked_branch import (
    SyncTrackedBranchUseCase,
)
from backend.infrastructure.database.models import Repository, RepositorySyncRun, Snapshot

if TYPE_CHECKING:
    from backend.features.change_requests.service import ChangeRequestCollectionService
    from backend.features.commit_catalog.service import CommitCatalogService
    from backend.features.repository_tags.service import RepositoryTagService


class RepositoryCollectionService:
    """Orchestrator for repository observation, tracked branch collection, and snapshots."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        git_client: RepositoryGitClient,
        publisher: CollectedSnapshotPublisher,
        sync_lease_seconds: int = 300,
        commit_catalog_service: CommitCatalogService | None = None,
        change_request_service: ChangeRequestCollectionService | None = None,
        tag_service: RepositoryTagService | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._git_client = git_client
        self._publisher = publisher
        self._sync_lease_seconds = sync_lease_seconds
        self._commit_catalog_service = commit_catalog_service
        self._change_request_service = change_request_service
        self._tag_service = tag_service

    @property
    def _observe_use_case(self) -> ObserveRepositoryUseCase:
        return ObserveRepositoryUseCase(
            sessionmaker=self._sessionmaker,
            ref_reader=self._git_client,
        )

    @property
    def _sync_branch_use_case(self) -> SyncTrackedBranchUseCase:
        return SyncTrackedBranchUseCase(
            sessionmaker=self._sessionmaker,
            object_fetcher=self._git_client,
            graph_reader=self._git_client,
            publisher=self._publisher,
        )

    @property
    def _sync_use_case(self) -> SyncRepositoryUseCase:
        return SyncRepositoryUseCase(
            sessionmaker=self._sessionmaker,
            ref_reader=self._git_client,
            sync_branch_use_case=self._sync_branch_use_case,
            sync_lease_seconds=self._sync_lease_seconds,
            commit_catalog_service=self._commit_catalog_service,
            change_request_service=self._change_request_service,
            tag_service=self._tag_service,
        )

    async def catalog_repository(self, repository_id: UUID) -> RepositoryCatalogResult:
        return await self._observe_use_case.catalog_repository(repository_id)

    async def validate_repository(self, repository_id: UUID) -> RepositoryCatalogResult:
        return await self._observe_use_case.validate_repository(repository_id)

    async def register_tracked_branch(
        self,
        request: TrackedBranchCreateRequest,
    ) -> TrackedBranchResponse:
        catalog = await self.validate_repository(request.repository_id)
        if not any(item.branch_ref == request.branch_ref for item in catalog.branches):
            raise CollectionError(
                reason="REPOSITORY_BRANCH_NOT_FOUND",
                detail="원격 Repository에서 선택한 Branch를 찾을 수 없습니다.",
                retryable=False,
                status_code=409,
            )
        async with self._sessionmaker() as session:
            try:
                tracked_branch = await RepositoryCollectionStore(session).create_tracked_branch(
                    request
                )
                await session.commit()
            except CollectionError:
                await session.rollback()
                raise
            except IntegrityError as exc:
                await session.rollback()
                raise CollectionError(
                    reason="TRACKED_BRANCH_CONFLICT",
                    detail=(
                        "같은 Repository/Branch 또는 VSS project ID가 이미 추적 대상으로 "
                        "등록되어 있습니다."
                    ),
                    retryable=False,
                    status_code=409,
                ) from exc
            except SQLAlchemyError as exc:
                await session.rollback()
                raise self._database_failure() from exc
            return TrackedBranchResponse.model_validate(tracked_branch)

    async def sync_repository(
        self,
        repository_id: UUID,
        *,
        trigger: SyncTrigger = "manual",
        request_id: UUID | None = None,
    ) -> RepositorySyncResult:
        return await self._sync_use_case.sync_repository(
            repository_id,
            trigger=trigger,
            request_id=request_id,
        )

    # --- Backward compatibility wrappers for internal tests / mocks ---
    async def _active_repository(self, repository_id: UUID) -> Repository:
        return await self._observe_use_case.get_active_repository(repository_id)

    async def _sync_branch(
        self,
        repository: Repository,
        *,
        tracked_branch_id: UUID,
        sync_run_id: UUID,
        request_id: UUID,
        remote_head: str | None,
    ) -> BranchSyncOutcome:
        async with self._sessionmaker() as session:
            sync_run = await session.get(RepositorySyncRun, sync_run_id)
            if sync_run is None:
                raise self._database_failure()
            lease_generation = sync_run.lease_generation
        return await self._sync_branch_use_case.sync_branch(
            repository,
            tracked_branch_id=tracked_branch_id,
            sync_run_id=sync_run_id,
            lease_generation=lease_generation,
            request_id=request_id,
            remote_head=remote_head,
        )

    async def _classify_change(
        self,
        repository_id: UUID,
        *,
        previous_head: str | None,
        observed_head: str,
        had_history: bool,
    ) -> BranchChangeType:
        return await self._sync_branch_use_case._classify_change(
            repository_id,
            previous_head=previous_head,
            observed_head=observed_head,
            had_history=had_history,
        )

    async def _claim_sync(
        self,
        repository_id: UUID,
        *,
        request_id: UUID,
        trigger: SyncTrigger,
    ) -> tuple[Repository, RepositorySyncRun]:
        return await self._sync_use_case._claim_sync(
            repository_id,
            request_id=request_id,
            trigger=trigger,
        )

    async def _refresh_lease(
        self,
        sync_run_id: UUID,
        *,
        expected_generation: int | None = None,
    ) -> int:
        return await self._sync_use_case._refresh_lease(
            sync_run_id,
            expected_generation=expected_generation,
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
        return await self._sync_use_case._finish_run(
            sync_run,
            outcomes=outcomes,
            ok=ok,
            reason=reason,
            detail=detail,
            retryable=retryable,
        )

    async def _finish_failed_run(
        self,
        sync_run: RepositorySyncRun,
        error: CollectionError,
        *,
        outcomes: list[BranchSyncOutcome],
    ) -> RepositorySyncResult:
        return await self._sync_use_case._finish_failed_run(
            sync_run,
            error,
            outcomes=outcomes,
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
        return SyncTrackedBranchUseCase._existing_snapshot_outcome(
            tracked_branch_id=tracked_branch_id,
            branch_ref=branch_ref,
            previous_head=previous_head,
            observed_head=observed_head,
            change_type=change_type,
            snapshot=snapshot,
        )

    @staticmethod
    def _database_failure() -> CollectionError:
        return CollectionError(
            reason="DATABASE_UNAVAILABLE",
            detail="Repository 수집용 Snapshot 데이터베이스를 사용할 수 없습니다.",
            retryable=True,
            status_code=503,
        )
