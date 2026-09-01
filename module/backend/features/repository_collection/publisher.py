"""수집된 exact revision을 Snapshot으로 materialize하고 VSS에 제출한다."""

from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from backend.features.materialization.errors import MaterializationError
from backend.features.repository_collection.errors import CollectionError
from backend.features.repository_collection.materializer import CollectedRevisionMaterializer
from backend.features.snapshots.store import SnapshotStore
from backend.infrastructure.database.models import Snapshot, TrackedBranch
from backend.integrations.vss.client import VssHttpClient
from backend.integrations.vss.errors import VssIntegrationError
from backend.integrations.vss.schemas import VssIndexRequest, VssStartIndexResponse


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    ok: bool
    reason: str
    detail: str
    retryable: bool
    snapshot_id: UUID
    snapshot_state: str


class CollectedSnapshotPublisher:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        materializer: CollectedRevisionMaterializer,
        vss_client: VssHttpClient,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._materializer = materializer
        self._vss_client = vss_client

    async def publish(self, snapshot_id: UUID, *, request_id: UUID) -> PublishOutcome:
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
                await self._record_materialization_failure(session, store, snapshot, exc)
                return PublishOutcome(
                    ok=False,
                    reason=exc.reason,
                    detail=exc.detail,
                    retryable=exc.retryable,
                    snapshot_id=snapshot.snapshot_id,
                    snapshot_state=snapshot.state,
                )

            try:
                snapshot.source_type = materialized.source_type
                await store.set_state(
                    snapshot,
                    "materialized",
                    materialized_locator=materialized.locator,
                )
                await store.set_state(snapshot, "submitting")
                attempt = await store.start_attempt(snapshot, request_id=request_id)
                await session.commit()
            except SQLAlchemyError as exc:
                await session.rollback()
                raise self._database_failure() from exc

            index_request = VssIndexRequest(
                project_root=str(materialized.project_root),
                project_id=snapshot.vss_project_id,
                force=False,
                briefing=True,
                note=f"snapshot {snapshot.target_revision}",
            )
            started = time.perf_counter()
            try:
                upstream = await run_in_threadpool(self._vss_client.start_index, index_request)
            except VssIntegrationError as exc:
                latency_ms = (time.perf_counter() - started) * 1000
                return await self._finish_vss_error(
                    session,
                    store,
                    snapshot,
                    attempt,
                    exc,
                    latency_ms,
                )

            latency_ms = (time.perf_counter() - started) * 1000
            return await self._finish_vss_result(
                session,
                store,
                snapshot,
                attempt,
                upstream,
                latency_ms,
            )

    async def _finish_vss_result(
        self,
        session: AsyncSession,
        store: SnapshotStore,
        snapshot: Snapshot,
        attempt,
        upstream: VssStartIndexResponse,
        latency_ms: float,
    ) -> PublishOutcome:
        result = upstream.result
        vss_state = result.state.value if result.state is not None else None
        result_json = {
            "accepted": result.accepted,
            "project_id": result.project_id,
            "state": vss_state,
            "reason": result.reason,
            "heartbeat_age_s": result.heartbeat_age_s,
            "fingerprint": result.fingerprint,
        }
        if result.accepted:
            state = "accepted"
            reason = "VSS_INDEX_ACCEPTED"
            detail = "새 Branch HEAD의 전체 revision 인덱싱을 VSS가 접수했습니다."
            retryable = False
            ok = True
        elif result.reason == "already_running":
            state = "rejected"
            reason = "VSS_INDEX_ALREADY_RUNNING"
            detail = "같은 VSS project의 인덱싱이 진행 중이어서 새 revision을 제출하지 않았습니다."
            retryable = True
            ok = False
        elif result.reason == "not_a_directory":
            state = "failed"
            reason = "SNAPSHOT_MATERIALIZATION_FAILED"
            detail = "VSS가 materialized project_root를 디렉터리로 확인하지 못했습니다."
            retryable = True
            ok = False
        else:
            state = "rejected"
            reason = "VSS_HTTP_REQUEST_REJECTED"
            detail = "VSS가 새 Branch HEAD 인덱싱 요청을 거부했습니다."
            retryable = False
            ok = False

        try:
            await store.finish_attempt(
                attempt,
                upstream_status_code=upstream.status_code,
                vss_state=vss_state,
                vss_reason=result.reason or reason,
                vss_detail=detail,
                retryable=retryable,
                latency_ms=latency_ms,
                result_json=result_json,
            )
            await store.set_state(
                snapshot,
                state,
                vss_state=vss_state,
                vss_reason=result.reason or reason,
                vss_detail=detail,
            )
            await session.commit()
        except SQLAlchemyError as exc:
            await session.rollback()
            raise self._database_failure() from exc
        return PublishOutcome(
            ok=ok,
            reason=reason,
            detail=detail,
            retryable=retryable,
            snapshot_id=snapshot.snapshot_id,
            snapshot_state=snapshot.state,
        )

    async def _finish_vss_error(
        self,
        session: AsyncSession,
        store: SnapshotStore,
        snapshot: Snapshot,
        attempt,
        error: VssIntegrationError,
        latency_ms: float,
    ) -> PublishOutcome:
        detail = "새 Branch HEAD를 VSS에 제출하지 못했습니다."
        try:
            await store.finish_attempt(
                attempt,
                upstream_status_code=error.upstream_status_code,
                vss_state=None,
                vss_reason=error.reason,
                vss_detail=detail,
                retryable=error.retryable,
                latency_ms=latency_ms,
                result_json=None,
            )
            await store.set_state(
                snapshot,
                "failed",
                vss_reason=error.reason,
                vss_detail=detail,
            )
            await session.commit()
        except SQLAlchemyError as exc:
            await session.rollback()
            raise self._database_failure() from exc
        return PublishOutcome(
            ok=False,
            reason=error.reason,
            detail=detail,
            retryable=error.retryable,
            snapshot_id=snapshot.snapshot_id,
            snapshot_state=snapshot.state,
        )

    async def _record_materialization_failure(
        self,
        session: AsyncSession,
        store: SnapshotStore,
        snapshot: Snapshot,
        error: CollectionError | MaterializationError,
    ) -> None:
        try:
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
    def _database_failure() -> CollectionError:
        return CollectionError(
            reason="COLLECTION_PERSIST_FAILED",
            detail="Repository 수집 결과를 Snapshot 데이터베이스에 기록하지 못했습니다.",
            retryable=True,
            status_code=500,
        )
