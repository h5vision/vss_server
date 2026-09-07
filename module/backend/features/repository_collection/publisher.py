"""수집된 exact revision을 immutable Snapshot으로 materialize한다.

Repository sync/materialization 경로는 VSS indexing을 시작하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from backend.features.materialization.errors import MaterializationError
from backend.features.repository_collection.errors import CollectionError
from backend.features.repository_collection.materializer import CollectedRevisionMaterializer
from backend.features.repository_collection.store import RepositoryCollectionStore
from backend.features.snapshots.store import SnapshotStore
from backend.infrastructure.database.models import Snapshot, TrackedBranch


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    ok: bool
    reason: str
    detail: str
    retryable: bool
    snapshot_id: UUID
    snapshot_state: str


class CollectedSnapshotPublisher:
    """Materializes collection-owned Snapshots without invoking VSS.

    The historical class name is retained during the strangler refactor to avoid a
    broad rename.  PR 9.2-B makes the ownership boundary explicit: Repository sync
    may prepare an immutable Snapshot, but only the Admin Index path may submit it
    to VSS.
    """

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        materializer: CollectedRevisionMaterializer,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._materializer = materializer

    async def publish(
        self,
        snapshot_id: UUID,
        *,
        request_id: UUID,
        sync_run_id: UUID | None = None,
        lease_generation: int | None = None,
    ) -> PublishOutcome:
        # request_id remains part of the transitional call contract.  Index attempts
        # are no longer created here; PR 9.2-C will create them from Admin Index.
        del request_id

        async with self._sessionmaker() as session:
            store = SnapshotStore(session)
            try:
                snapshot = await store.get(snapshot_id)
                if snapshot is None or snapshot.tracked_branch_id is None:
                    raise CollectionError(
                        reason="COLLECTION_SNAPSHOT_NOT_FOUND",
                        detail="수집으로 생성된 Snapshot을 찾을 수 없습니다.",
                        retryable=False,
                        status_code=404,
                    )
                tracked_branch = await session.get(TrackedBranch, snapshot.tracked_branch_id)
                if tracked_branch is None:
                    raise CollectionError(
                        reason="TRACKED_BRANCH_NOT_FOUND",
                        detail="Snapshot의 추적 Branch를 찾을 수 없습니다.",
                        retryable=False,
                        status_code=409,
                    )
                await self._assert_sync_owner(
                    session,
                    sync_run_id=sync_run_id,
                    lease_generation=lease_generation,
                )

                # A completed materialization is immutable and idempotent.  Sync must
                # never move a materialized/indexing/completed Snapshot backwards.
                if snapshot.materialized_locator is not None:
                    return PublishOutcome(
                        ok=True,
                        reason="SNAPSHOT_ALREADY_MATERIALIZED",
                        detail="동일 exact revision의 immutable Snapshot이 이미 준비되어 있습니다.",
                        retryable=False,
                        snapshot_id=snapshot.snapshot_id,
                        snapshot_state=snapshot.state,
                    )

                if snapshot.state not in {"validated", "materializing"}:
                    raise CollectionError(
                        reason="SNAPSHOT_NOT_MATERIALIZABLE",
                        detail=(
                            "현재 Snapshot 상태에서는 Repository sync가 materialization을 "
                            "시작할 수 없습니다."
                        ),
                        retryable=False,
                        status_code=409,
                    )

                await store.set_state(snapshot, "materializing")
                await session.commit()
            except CollectionError:
                raise
            except SQLAlchemyError as exc:
                await session.rollback()
                raise self._database_failure() from exc

            try:
                materialized = await run_in_threadpool(
                    self._materializer.materialize,
                    repository_id=snapshot.repository_id,
                    tracked_branch_id=tracked_branch.tracked_branch_id,
                    snapshot_id=snapshot.snapshot_id,
                    target_revision=snapshot.target_revision,
                )
            except (CollectionError, MaterializationError) as exc:
                await self._record_materialization_failure(
                    session,
                    store,
                    snapshot,
                    exc,
                    sync_run_id=sync_run_id,
                    lease_generation=lease_generation,
                )
                return PublishOutcome(
                    ok=False,
                    reason=exc.reason,
                    detail=exc.detail,
                    retryable=exc.retryable,
                    snapshot_id=snapshot.snapshot_id,
                    snapshot_state=snapshot.state,
                )

            try:
                await self._assert_sync_owner(
                    session,
                    sync_run_id=sync_run_id,
                    lease_generation=lease_generation,
                )
                snapshot.source_type = materialized.source_type
                await store.set_state(
                    snapshot,
                    "materialized",
                    materialized_locator=materialized.locator,
                )
                await session.commit()
            except SQLAlchemyError as exc:
                await session.rollback()
                raise self._database_failure() from exc

            return PublishOutcome(
                ok=True,
                reason="SNAPSHOT_MATERIALIZED",
                detail=(
                    "immutable exact Snapshot materialization을 완료했습니다. "
                    "VSS 인덱싱은 Admin의 명시적 Index 요청에서만 시작합니다."
                ),
                retryable=False,
                snapshot_id=snapshot.snapshot_id,
                snapshot_state=snapshot.state,
            )

    async def _record_materialization_failure(
        self,
        session: AsyncSession,
        store: SnapshotStore,
        snapshot: Snapshot,
        error: CollectionError | MaterializationError,
        *,
        sync_run_id: UUID | None,
        lease_generation: int | None,
    ) -> None:
        try:
            await self._assert_sync_owner(
                session,
                sync_run_id=sync_run_id,
                lease_generation=lease_generation,
            )
            # vss_reason/vss_detail are legacy columns currently used by the Admin UI
            # for structured failure diagnostics.  No VSS request occurs here.
            await store.set_state(
                snapshot,
                "failed",
                vss_reason=error.reason,
                vss_detail=error.detail,
            )
            await session.commit()
        except SQLAlchemyError as exc:
            await session.rollback()
            raise self._database_failure() from exc

    @staticmethod
    async def _assert_sync_owner(
        session: AsyncSession,
        *,
        sync_run_id: UUID | None,
        lease_generation: int | None,
    ) -> None:
        if sync_run_id is None and lease_generation is None:
            return
        if sync_run_id is None or lease_generation is None:
            raise CollectionError(
                reason="COLLECTION_SYNC_FENCING_TOKEN_INVALID",
                detail="Snapshot materialization fencing context가 불완전합니다.",
                retryable=False,
                status_code=409,
            )
        await RepositoryCollectionStore(session).assert_sync_owner(
            sync_run_id,
            expected_generation=lease_generation,
        )

    @staticmethod
    def _database_failure() -> CollectionError:
        return CollectionError(
            reason="COLLECTION_PERSIST_FAILED",
            detail="Repository 수집 결과를 Snapshot 데이터베이스에 기록하지 못했습니다.",
            retryable=True,
            status_code=500,
        )
