"""향후 인증된 Admin route에서만 호출할 동일 Snapshot 재시도 서비스."""

from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from backend.core.errors import ApiError
from backend.core.orchestration import MODULE_PUSH, VSS_PULL, IndexOrchestrationMode
from backend.features.indexing.project_root import IndexProjectRootResolver
from backend.features.materialization.service import SnapshotMaterializer
from backend.features.snapshots.schemas import SnapshotRetryResponse
from backend.features.snapshots.store import SnapshotStore
from backend.infrastructure.database.models import Snapshot, SnapshotAttempt
from backend.integrations.vss.client import VssHttpClient
from backend.integrations.vss.errors import VssIntegrationError
from backend.integrations.vss.schemas import VssIndexRequest, VssIndexState, VssStartIndexResponse
from backend.ports.git import ManagedRepositoryWorkspace


@dataclass(frozen=True, slots=True)
class RetryOutcome:
    status_code: int
    body: SnapshotRetryResponse


class SnapshotRetryService:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        materializer: SnapshotMaterializer,
        vss_client: VssHttpClient,
        workspace_manager: ManagedRepositoryWorkspace | None = None,
        index_orchestration_mode: IndexOrchestrationMode = MODULE_PUSH,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._vss_client = vss_client
        self._project_root_resolver = IndexProjectRootResolver(
            materializer=materializer,
            workspace_manager=workspace_manager,
        )
        self._index_orchestration_mode = index_orchestration_mode

    async def retry(self, snapshot_id: UUID, *, request_id: UUID) -> RetryOutcome:
        if self._index_orchestration_mode == VSS_PULL:
            raise ApiError(
                status_code=409,
                reason="VSS_PULL_OWNS_INDEX_START",
                detail=(
                    "현재 배포에서는 VSS가 내부 source API를 pull하여 인덱싱을 "
                    "시작합니다. Module은 VSS 인덱싱 재시도를 제출하지 않습니다."
                ),
                retryable=False,
            )
        async with self._sessionmaker() as session:
            store = SnapshotStore(session)
            try:
                # 상태 확인부터 attempt 생성까지 한 Snapshot row를 직렬화한다. 동시에 들어온
                # Admin 재시도가 같은 attempt_number로 VSS를 중복 호출하는 것을 막는다.
                snapshot = await store.get_for_update(snapshot_id)
            except SQLAlchemyError as exc:
                raise self._database_unavailable() from exc
            if snapshot is None:
                raise ApiError(
                    status_code=404,
                    reason="SNAPSHOT_NOT_FOUND",
                    detail="재시도할 Snapshot을 찾을 수 없습니다.",
                    retryable=False,
                )
            tracked_branch = await self._project_root_resolver.lock_tracked_branch(
                session,
                snapshot,
            )
            if snapshot.state in {"completed", "already_indexed"}:
                return RetryOutcome(
                    status_code=200,
                    body=self._response(
                        snapshot,
                        request_id,
                        reason="TARGET_ALREADY_INDEXED",
                        detail="동일 target revision이 이미 완료되어 재시도하지 않았습니다.",
                        retryable=False,
                    ),
                )
            if snapshot.state not in {"failed", "rejected", "aborted"}:
                raise ApiError(
                    status_code=409,
                    reason="SNAPSHOT_RETRY_NOT_ALLOWED",
                    detail="현재 Snapshot 상태에서는 재시도할 수 없습니다.",
                    retryable=False,
                    extra=self._snapshot_extra(snapshot),
                )
            await self._project_root_resolver.assert_no_other_active_snapshot(
                session,
                snapshot,
            )
            verified_materialized = await self._project_root_resolver.verify_materialized(snapshot)
            try:
                status = await run_in_threadpool(
                    self._vss_client.status,
                    snapshot.vss_project_id,
                )
            except VssIntegrationError as exc:
                raise ApiError(
                    status_code=503 if exc.retryable else 502,
                    reason=exc.reason,
                    detail="재시도 전 VSS 상태를 확인하지 못했습니다.",
                    retryable=exc.retryable,
                    extra=self._snapshot_extra(snapshot),
                ) from exc

            # 같은 VSS project의 작업과 경쟁하면 어느 revision이 승격될지 불명확하므로
            # 실행 중 Job이 하나라도 있으면 새로운 attempt를 만들기 전에 차단한다.
            if status.state in {
                VssIndexState.RUNNING,
                VssIndexState.INDEXING_LEXICAL,
                VssIndexState.PROMOTING,
            }:
                raise ApiError(
                    status_code=409,
                    reason="VSS_INDEX_ALREADY_RUNNING",
                    detail="같은 VSS project의 인덱싱이 진행 중이어서 재시도하지 않았습니다.",
                    retryable=True,
                    extra=self._snapshot_extra(snapshot),
                )
            target_already_indexed = status.completed_for(snapshot.target_revision)
            active_revision = (
                status.index.commit
                if status.state is VssIndexState.DONE and status.index is not None
                else None
            )
            needs_exists = status.state is VssIndexState.NONE or (
                status.state is VssIndexState.DONE and active_revision is None
            )
            if needs_exists:
                # VSS 재시작으로 Job 상태가 사라져도 active index는 남을 수 있다. exact
                # target이면 재제출하지 않고 완료로 수렴해 불필요한 인덱싱을 피한다.
                try:
                    exists = await run_in_threadpool(
                        self._vss_client.exists,
                        snapshot.vss_project_id,
                    )
                except VssIntegrationError as exc:
                    raise ApiError(
                        status_code=503 if exc.retryable else 502,
                        reason=exc.reason,
                        detail="재시도 전 VSS active index를 확인하지 못했습니다.",
                        retryable=exc.retryable,
                        extra=self._snapshot_extra(snapshot),
                    ) from exc
                if exists.exists:
                    active_revision = exists.commit
                target_already_indexed = (
                    exists.exists and exists.commit == snapshot.target_revision
                )
            if target_already_indexed:
                try:
                    await store.set_state(
                        snapshot,
                        "completed",
                        vss_state=status.state.value,
                        vss_reason="TARGET_ALREADY_INDEXED",
                        vss_detail="VSS active index가 Snapshot target revision과 일치합니다.",
                    )
                    await session.commit()
                except SQLAlchemyError as exc:
                    await session.rollback()
                    raise self._result_persist_failed(snapshot) from exc
                return RetryOutcome(
                    status_code=200,
                    body=self._response(
                        snapshot,
                        request_id,
                        reason="TARGET_ALREADY_INDEXED",
                        detail=(
                            "VSS active index가 target revision과 일치하여 "
                            "재시도하지 않았습니다."
                        ),
                        retryable=False,
                    ),
                )

            # VSS running/idempotency 확인 뒤에만 mutable Branch working copy를 exact HEAD로
            # refresh하여 기존 비동기 VSS Indexer가 읽는 project_root를 안정적으로 유지한다.
            resolved_root = await self._project_root_resolver.resolve(
                session,
                snapshot,
                verified_materialized=verified_materialized,
                locked_tracked_branch=tracked_branch,
            )

            note_prefix = "branch" if resolved_root.source == "managed_branch" else "snapshot"
            full_request = VssIndexRequest(
                project_root=str(resolved_root.project_root),
                project_id=snapshot.vss_project_id,
                force=False,
                briefing=True,
                note=f"{note_prefix} {snapshot.target_revision}",
            )
            # 실제 VSS 제출을 수행할 때만 attempt를 증가시킨다. 상태 확인만으로는
            # 운영 이력을 부풀리지 않으며, 재시도도 항상 force=false를 유지한다.
            try:
                await store.set_state(snapshot, "submitting")
                attempt = await store.start_attempt(snapshot, request_id=request_id)
                await session.commit()
            except SQLAlchemyError as exc:
                await session.rollback()
                raise self._result_persist_failed(snapshot) from exc

            started = time.perf_counter()
            try:
                upstream = await run_in_threadpool(self._vss_client.start_index, full_request)
            except VssIntegrationError as exc:
                await self._finish_exception(
                    session,
                    store,
                    snapshot,
                    attempt,
                    exc,
                    (time.perf_counter() - started) * 1000,
                )
                raise ApiError(
                    status_code=503 if exc.retryable else 502,
                    reason=exc.reason,
                    detail="VSS 재시도 요청을 완료하지 못했습니다.",
                    retryable=exc.retryable,
                    extra=self._snapshot_extra(snapshot),
                ) from exc
            return await self._finish_result(
                session,
                store,
                snapshot,
                attempt,
                upstream,
                (time.perf_counter() - started) * 1000,
                request_id,
            )

    async def _finish_result(
        self,
        session: AsyncSession,
        store: SnapshotStore,
        snapshot: Snapshot,
        attempt: SnapshotAttempt,
        upstream: VssStartIndexResponse,
        latency_ms: float,
        request_id: UUID,
    ) -> RetryOutcome:
        result = upstream.result
        vss_state = result.state.value if result.state is not None else None
        result_json = {
            "accepted": result.accepted,
            "project_id": result.project_id,
            "state": vss_state,
            "reason": result.reason,
            "heartbeat_age_s": result.heartbeat_age_s,
            "fingerprint": result.fingerprint,
            "submission_mode": "vss_auto",
        }
        if result.accepted:
            state = "accepted"
            reason = "VSS_INDEX_RETRY_ACCEPTED"
            detail = (
                "동일 Snapshot의 VSS 인덱싱 재시도가 접수됐습니다. "
                "full/incremental 모드는 VSS가 자체 판정합니다."
            )
            retryable = False
            status_code = 202
        elif result.reason == "already_indexed":
            state = "completed"
            reason = "TARGET_ALREADY_INDEXED"
            detail = "VSS가 target revision을 이미 active index로 확인했습니다."
            retryable = False
            status_code = 200
        elif result.reason == "already_running":
            state = "indexing"
            reason = "VSS_INDEX_ALREADY_RUNNING"
            detail = "같은 VSS project 작업이 진행 중이어서 새 재시도를 제출하지 않았습니다."
            retryable = True
            status_code = 409
        elif result.reason == "not_a_directory":
            state = "failed"
            reason = "SNAPSHOT_MATERIALIZATION_FAILED"
            detail = "VSS가 재사용한 project_root를 디렉터리로 확인하지 못했습니다."
            retryable = True
            status_code = 500
        else:
            state = "rejected"
            reason = "VSS_HTTP_REQUEST_REJECTED"
            detail = "VSS가 Snapshot 재시도 요청을 거부했습니다."
            retryable = False
            status_code = 502

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
            raise self._result_persist_failed(snapshot) from exc

        if not result.accepted and reason != "TARGET_ALREADY_INDEXED":
            raise ApiError(
                status_code=status_code,
                reason=reason,
                detail=detail,
                retryable=retryable,
                extra={**self._snapshot_extra(snapshot), "vss_reason": result.reason},
            )
        return RetryOutcome(
            status_code=status_code,
            body=self._response(
                snapshot,
                request_id,
                reason=reason,
                detail=detail,
                retryable=retryable,
            ),
        )

    async def _finish_exception(
        self,
        session: AsyncSession,
        store: SnapshotStore,
        snapshot: Snapshot,
        attempt: SnapshotAttempt,
        exc: VssIntegrationError,
        latency_ms: float,
    ) -> None:
        detail = "VSS 재시도 요청을 완료하지 못했습니다."
        try:
            await store.finish_attempt(
                attempt,
                upstream_status_code=exc.upstream_status_code,
                vss_state=None,
                vss_reason=exc.reason,
                vss_detail=detail,
                retryable=exc.retryable,
                latency_ms=latency_ms,
                result_json={"submission_mode": "vss_auto"},
            )
            await store.set_state(
                snapshot,
                "failed",
                vss_reason=exc.reason,
                vss_detail=detail,
            )
            await session.commit()
        except SQLAlchemyError as persist_exc:
            await session.rollback()
            raise self._result_persist_failed(snapshot) from persist_exc

    @staticmethod
    def _response(
        snapshot: Snapshot,
        request_id: UUID,
        *,
        reason: str,
        detail: str,
        retryable: bool,
    ) -> SnapshotRetryResponse:
        return SnapshotRetryResponse(
            reason=reason,
            detail=detail,
            retryable=retryable,
            request_id=request_id,
            snapshot_id=snapshot.snapshot_id,
            state=snapshot.state,
            attempt_count=snapshot.attempt_count,
        )

    @staticmethod
    def _snapshot_extra(snapshot: Snapshot) -> dict[str, str]:
        return {
            "snapshot_id": str(snapshot.snapshot_id),
            "project_id": snapshot.vss_project_id,
            "state": snapshot.state,
            "target_revision": snapshot.target_revision,
        }

    @staticmethod
    def _database_unavailable() -> ApiError:
        return ApiError(
            status_code=503,
            reason="DATABASE_UNAVAILABLE",
            detail="Snapshot 데이터베이스를 사용할 수 없습니다.",
            retryable=True,
        )

    @classmethod
    def _result_persist_failed(cls, snapshot: Snapshot) -> ApiError:
        return ApiError(
            status_code=500,
            reason="SNAPSHOT_RESULT_PERSIST_FAILED",
            detail="Snapshot 재시도 결과를 저장하지 못했습니다.",
            retryable=True,
            extra=cls._snapshot_extra(snapshot),
        )
