"""사용자가 선택한 Branch만 수집하고 새 HEAD를 Snapshot/VSS 흐름에 연결한다."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from backend.features.change_requests.errors import ChangeRequestError
from backend.features.commit_catalog.errors import CommitCatalogError
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
from backend.features.snapshots.store import SnapshotStore
from backend.infrastructure.database.models import Repository, RepositorySyncRun, Snapshot

if TYPE_CHECKING:
    from backend.features.change_requests.service import ChangeRequestCollectionService
    from backend.features.commit_catalog.service import CommitCatalogService
    from backend.features.repository_tags.service import RepositoryTagService


class RepositoryCollectionService:
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

    async def catalog_repository(self, repository_id: UUID) -> RepositoryCatalogResult:
        repository = await self._active_repository(repository_id)
        branches = await run_in_threadpool(
            self._git_client.list_remote_heads,
            repository.remote_url,
        )
        return RepositoryCatalogResult(
            repository_id=repository.repository_id,
            default_branch_ref=repository.default_branch_ref,
            default_branch_exists=any(
                item.branch_ref == repository.default_branch_ref for item in branches
            ),
            branches=branches,
        )

    async def validate_repository(self, repository_id: UUID) -> RepositoryCatalogResult:
        catalog = await self.catalog_repository(repository_id)
        if not catalog.default_branch_exists:
            raise CollectionError(
                reason="REPOSITORY_DEFAULT_BRANCH_NOT_FOUND",
                detail="Repository의 기본 Branch를 원격에서 찾을 수 없습니다.",
                retryable=False,
                status_code=409,
            )
        return catalog

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
                self._git_client.list_remote_heads,
                repository.remote_url,
            )
            heads_by_ref = {item.branch_ref: item.commit_sha for item in remote_heads}
            tracked_branch_ids = await self._tracked_branch_ids(repository_id)
            for tracked_branch_id in tracked_branch_ids:
                try:
                    outcome = await self._sync_branch(
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
        if self._tag_service is not None:
            try:
                await self._tag_service.sync_repository(
                    repository_id,
                    sync_run_id=sync_run.sync_run_id,
                    progress=lambda: self._refresh_lease(sync_run.sync_run_id),
                )
            except CollectionError as exc:
                tag_failure = exc
        change_request_failure = None
        if (
            self._change_request_service is not None
            and self._change_request_service.supports(repository.provider)
        ):
            try:
                await self._change_request_service.sync_repository(
                    repository_id,
                    progress=lambda: self._refresh_lease(sync_run.sync_run_id),
                )
            except ChangeRequestError as exc:
                change_request_failure = exc
        catalog_failure = None
        if self._commit_catalog_service is not None:
            try:
                await self._commit_catalog_service.catalog_repository(
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
            store = RepositoryCollectionStore(session)
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
            self._git_client.fetch_branch,
            repository_id=repository.repository_id,
            tracked_branch_id=tracked_branch_id,
            remote_url=repository.remote_url,
            branch_ref=branch_ref,
        )
        observed_at = datetime.now(timezone.utc)

        async with self._sessionmaker() as session:
            store = RepositoryCollectionStore(session)
            tracked_branch = await store.get_tracked_branch(tracked_branch_id)
            previous_head = tracked_branch.current_head_sha
            if previous_head == observed_head:
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
                    published = await self._publisher.publish(
                        snapshot.snapshot_id,
                        request_id=request_id,
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
                        "선택한 Branch의 HEAD SHA가 이전 관측과 같아 "
                        "새 Snapshot을 만들지 않았습니다."
                    ),
                    retryable=False,
                    tracked_branch_id=tracked_branch_id,
                    branch_ref=branch_ref,
                    previous_head_sha=previous_head,
                    observed_head_sha=observed_head,
                )

            had_history = await store.has_head_history(tracked_branch_id)
            change_type = await self._classify_change(
                repository.repository_id,
                previous_head=previous_head,
                observed_head=observed_head,
                had_history=had_history,
            )
            snapshot_store = SnapshotStore(session)
            existing = await snapshot_store.find_by_target(
                tracked_branch.vss_project_id,
                observed_head,
            )
            await store.observe_head(
                tracked_branch,
                sync_run_id=sync_run_id,
                previous_head_sha=previous_head,
                observed_head_sha=observed_head,
                change_type=change_type,
                observed_at=observed_at,
            )
            snapshot = existing
            if snapshot is None:
                snapshot = await snapshot_store.create_from_collection(
                    request_id=request_id,
                    tracked_branch=tracked_branch,
                    base_revision=previous_head or observed_head,
                    target_revision=observed_head,
                )
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise CollectionError(
                    reason="COLLECTION_SNAPSHOT_CONFLICT",
                    detail="같은 VSS project와 revision의 Snapshot이 동시에 생성되었습니다.",
                    retryable=True,
                    status_code=409,
                ) from exc
            except SQLAlchemyError as exc:
                await session.rollback()
                raise self._database_failure() from exc

        if existing is not None:
            return self._existing_snapshot_outcome(
                tracked_branch_id=tracked_branch_id,
                branch_ref=branch_ref,
                previous_head=previous_head,
                observed_head=observed_head,
                change_type=change_type,
                snapshot=existing,
            )
        published = await self._publisher.publish(snapshot.snapshot_id, request_id=request_id)
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

    async def _observe_deleted_branch(
        self,
        session: AsyncSession,
        store: RepositoryCollectionStore,
        tracked_branch,
        *,
        sync_run_id: UUID,
    ) -> BranchSyncOutcome:
        observed_at = datetime.now(timezone.utc)
        previous_head = tracked_branch.current_head_sha
        had_history = await store.has_head_history(tracked_branch.tracked_branch_id)
        if previous_head is None:
            await store.mark_unchanged(tracked_branch, observed_at=observed_at)
            await session.commit()
            if had_history:
                return BranchSyncOutcome(
                    ok=True,
                    reason="BRANCH_STILL_DELETED",
                    detail="삭제 상태인 Branch가 원격에 다시 생성되지 않았습니다.",
                    retryable=False,
                    tracked_branch_id=tracked_branch.tracked_branch_id,
                    branch_ref=tracked_branch.branch_ref,
                )
            return BranchSyncOutcome(
                ok=False,
                reason="REPOSITORY_BRANCH_NOT_FOUND",
                detail="선택한 Branch를 원격 Repository에서 찾을 수 없습니다.",
                retryable=True,
                tracked_branch_id=tracked_branch.tracked_branch_id,
                branch_ref=tracked_branch.branch_ref,
            )

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
            self._git_client.is_ancestor,
            repository_id,
            previous_head,
            observed_head,
        )
        return "fast_forward" if is_ancestor else "rewind"

    async def _claim_sync(
        self,
        repository_id: UUID,
        *,
        request_id: UUID,
        trigger: SyncTrigger,
    ) -> tuple[Repository, RepositorySyncRun]:
        async with self._sessionmaker() as session:
            try:
                repository, sync_run = await RepositoryCollectionStore(session).claim_sync(
                    repository_id,
                    request_id=request_id,
                    trigger=trigger,
                    lease_seconds=self._sync_lease_seconds,
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

    async def _tracked_branch_ids(self, repository_id: UUID) -> list[UUID]:
        async with self._sessionmaker() as session:
            try:
                branches = await RepositoryCollectionStore(session).list_tracked_branches(
                    repository_id
                )
                return [item.tracked_branch_id for item in branches]
            except SQLAlchemyError as exc:
                raise self._database_failure() from exc

    async def _branch_ref(self, tracked_branch_id: UUID) -> str:
        async with self._sessionmaker() as session:
            try:
                branch = await RepositoryCollectionStore(session).get_tracked_branch(
                    tracked_branch_id
                )
                return branch.branch_ref
            except SQLAlchemyError as exc:
                raise self._database_failure() from exc

    async def _active_repository(self, repository_id: UUID) -> Repository:
        async with self._sessionmaker() as session:
            try:
                repository = await session.get(Repository, repository_id)
            except SQLAlchemyError as exc:
                raise self._database_failure() from exc
            if repository is None:
                raise CollectionError(
                    reason="REPOSITORY_NOT_FOUND",
                    detail="요청한 Repository를 찾을 수 없습니다.",
                    retryable=False,
                    status_code=404,
                )
            if not repository.active:
                raise CollectionError(
                    reason="REPOSITORY_INACTIVE",
                    detail="비활성 Repository는 조회하거나 동기화할 수 없습니다.",
                    retryable=False,
                    status_code=409,
                )
            return repository

    async def _refresh_lease(self, sync_run_id: UUID) -> None:
        async with self._sessionmaker() as session:
            try:
                sync_run = await session.get(RepositorySyncRun, sync_run_id)
                if sync_run is None or sync_run.state != "running":
                    raise CollectionError(
                        reason="COLLECTION_SYNC_LEASE_LOST",
                        detail="Repository 동기화 lease를 유지할 수 없습니다.",
                        retryable=True,
                        status_code=409,
                    )
                await RepositoryCollectionStore(session).refresh_lease(
                    sync_run,
                    lease_seconds=self._sync_lease_seconds,
                )
                await session.commit()
            except CollectionError:
                await session.rollback()
                raise
            except SQLAlchemyError as exc:
                await session.rollback()
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
        async with self._sessionmaker() as session:
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

    @staticmethod
    def _database_failure() -> CollectionError:
        return CollectionError(
            reason="DATABASE_UNAVAILABLE",
            detail="Repository 수집용 Snapshot 데이터베이스를 사용할 수 없습니다.",
            retryable=True,
            status_code=503,
        )
