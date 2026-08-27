"""Internal retry service; an authenticated Admin route may call it later."""

from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from backend.core.errors import ApiError
from backend.features.materialization.errors import MaterializationError
from backend.features.materialization.service import SnapshotMaterializer
from backend.features.snapshots.schemas import SnapshotRetryResponse
from backend.features.snapshots.store import SnapshotStore
from backend.infrastructure.database.models import Snapshot, SnapshotAttempt
from backend.integrations.vss.client import VssHttpClient
from backend.integrations.vss.errors import VssIntegrationError
from backend.integrations.vss.schemas import (
    VssIndexRequest,
    VssIndexState,
    VssStartIndexResponse,
)


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
    ) -> None:
        self._sessionmaker = sessionmaker
        self._materializer = materializer
        self._vss_client = vss_client

    async def retry(self, snapshot_id: UUID, *, request_id: UUID) -> RetryOutcome:
        async with self._sessionmaker() as session:
            store = SnapshotStore(session)
            try:
                snapshot = await store.get(snapshot_id)
            except SQLAlchemyError as exc:
                raise self._database_unavailable() from exc
            if snapshot is None:
                raise ApiError(
                    status_code=404,
                    reason="SNAPSHOT_NOT_FOUND",
                    detail="재시도할 Snapshot을 찾을 수 없습니다.",
                    retryable=False,
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
            if snapshot.materialized_locator is None:
                raise ApiError(
                    status_code=409,
                    reason="SNAPSHOT_RETRY_MATERIALIZATION_REQUIRED",
                    detail="검증된 immutable revision이 없어 VSS 재시도를 시작할 수 없습니다.",
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
            if status.state is VssIndexState.NONE:
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

            try:
                await store.set_state(snapshot, "submitting")
                attempt = await store.start_attempt(snapshot, request_id=request_id)
                await session.commit()
            except SQLAlchemyError as exc:
                await session.rollback()
                raise self._result_persist_failed(snapshot) from exc

            request = VssIndexRequest(
                project_root=str(materialized.project_root),
                project_id=snapshot.vss_project_id,
                force=False,
                briefing=True,
                note=f"snapshot {snapshot.target_revision}",
            )
            started = time.perf_counter()
            try:
                upstream = await run_in_threadpool(self._vss_client.start_index, request)
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
        }
        if result.accepted:
            state = "accepted"
            reason = "VSS_INDEX_RETRY_ACCEPTED"
            detail = "동일 Snapshot의 VSS 인덱싱 재시도가 접수됐습니다."
            retryable = False
            status_code = 202
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

        if not result.accepted:
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
                result_json=None,
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
