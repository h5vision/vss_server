"""Synchronize Snapshot state with the authoritative VSS HTTP status."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from backend.core.errors import ApiError
from backend.features.indexing.schemas import IndexStatusResponse, VssProgressResponse
from backend.features.repositories.store import BranchBindingStore, StoreLookupError
from backend.features.snapshots.store import SnapshotStore
from backend.infrastructure.database.models import Snapshot
from backend.integrations.vss.client import VssHttpClient
from backend.integrations.vss.errors import VssIntegrationError
from backend.integrations.vss.schemas import VssIndexState, VssIndexStatus


@dataclass(frozen=True, slots=True)
class StatusDecision:
    state: str
    reason: str
    detail: str
    retryable: bool


class IndexStatusService:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        vss_client: VssHttpClient,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._vss_client = vss_client

    async def read_for_project(
        self,
        project_id: str,
        *,
        request_id: UUID,
    ) -> IndexStatusResponse:
        async with self._sessionmaker() as session:
            try:
                binding = await BranchBindingStore(session).resolve_active(project_id)
                snapshot = await SnapshotStore(session).latest_for_binding(binding.binding_id)
            except StoreLookupError as exc:
                raise ApiError(
                    status_code=409,
                    reason=exc.reason,
                    detail=exc.detail,
                    retryable=exc.retryable,
                ) from exc
            except SQLAlchemyError as exc:
                raise self._database_unavailable() from exc
            if snapshot is None:
                raise ApiError(
                    status_code=404,
                    reason="SNAPSHOT_NOT_FOUND",
                    detail="해당 project의 Snapshot 이력이 없습니다.",
                    retryable=False,
                )
            return await self.synchronize_snapshot(
                session,
                snapshot,
                request_id=request_id,
            )

    async def synchronize_by_id(
        self,
        snapshot_id: UUID,
        *,
        request_id: UUID,
    ) -> IndexStatusResponse:
        async with self._sessionmaker() as session:
            try:
                snapshot = await SnapshotStore(session).get(snapshot_id)
            except SQLAlchemyError as exc:
                raise self._database_unavailable() from exc
            if snapshot is None:
                raise ApiError(
                    status_code=404,
                    reason="SNAPSHOT_NOT_FOUND",
                    detail="요청한 Snapshot을 찾을 수 없습니다.",
                    retryable=False,
                )
            return await self.synchronize_snapshot(
                session,
                snapshot,
                request_id=request_id,
            )

    async def synchronize_snapshot(
        self,
        session: AsyncSession,
        snapshot: Snapshot,
        *,
        request_id: UUID,
    ) -> IndexStatusResponse:
        try:
            status = await run_in_threadpool(
                self._vss_client.status,
                snapshot.vss_project_id,
            )
            decision = await self._decide(snapshot, status)
        except VssIntegrationError as exc:
            raise ApiError(
                status_code=503 if exc.retryable else 502,
                reason=exc.reason,
                detail="VSS 인덱싱 상태를 조회하지 못했습니다.",
                retryable=exc.retryable,
                extra=self._snapshot_extra(snapshot),
            ) from exc

        store = SnapshotStore(session)
        try:
            await store.set_state(
                snapshot,
                decision.state,
                vss_state=status.state.value,
                vss_reason=decision.reason,
                vss_detail=decision.detail,
            )
            await session.commit()
        except SQLAlchemyError as exc:
            await session.rollback()
            raise ApiError(
                status_code=500,
                reason="SNAPSHOT_RESULT_PERSIST_FAILED",
                detail="VSS 상태 동기화 결과를 저장하지 못했습니다.",
                retryable=True,
                extra=self._snapshot_extra(snapshot),
            ) from exc

        return IndexStatusResponse(
            reason=decision.reason,
            detail=decision.detail,
            retryable=decision.retryable,
            request_id=request_id,
            snapshot_id=snapshot.snapshot_id,
            project_id=snapshot.vss_project_id,
            state=decision.state,
            target_revision=snapshot.target_revision,
            vss=VssProgressResponse(
                state=status.state.value,
                processed=status.processed,
                total=status.total,
                chunk_count=status.chunk_count,
            ),
        )

    async def _decide(self, snapshot: Snapshot, status: VssIndexStatus) -> StatusDecision:
        if status.state in {
            VssIndexState.RUNNING,
            VssIndexState.INDEXING_LEXICAL,
            VssIndexState.PROMOTING,
        }:
            return StatusDecision(
                state="indexing",
                reason="VSS_INDEX_IN_PROGRESS",
                detail="VSS가 Snapshot target revision을 인덱싱하고 있습니다.",
                retryable=False,
            )
        if status.state is VssIndexState.DONE:
            if status.completed_for(snapshot.target_revision):
                return StatusDecision(
                    state="completed",
                    reason="VSS_INDEX_COMPLETED",
                    detail="VSS 인덱싱이 target revision과 일치하는 상태로 완료됐습니다.",
                    retryable=False,
                )
            return self._revision_mismatch()
        if status.state is VssIndexState.FAILED:
            return StatusDecision(
                state="failed",
                reason="VSS_INDEX_FAILED",
                detail="VSS 인덱싱 작업이 실패했습니다.",
                retryable=True,
            )
        if status.state is VssIndexState.ABORTED:
            return StatusDecision(
                state="aborted",
                reason="VSS_INDEX_ABORTED",
                detail="VSS 인덱싱 작업이 중단됐습니다.",
                retryable=True,
            )

        exists = await run_in_threadpool(
            self._vss_client.exists,
            snapshot.vss_project_id,
        )
        if exists.exists and exists.commit == snapshot.target_revision:
            return StatusDecision(
                state="completed",
                reason="TARGET_ALREADY_INDEXED",
                detail="VSS active index가 Snapshot target revision과 일치합니다.",
                retryable=False,
            )
        if exists.exists:
            return self._revision_mismatch()
        return StatusDecision(
            state="failed",
            reason="VSS_INDEX_STATUS_MISSING",
            detail="VSS에서 Snapshot 인덱싱 상태와 active index를 찾을 수 없습니다.",
            retryable=True,
        )

    @staticmethod
    def _revision_mismatch() -> StatusDecision:
        return StatusDecision(
            state="failed",
            reason="VSS_REVISION_MISMATCH",
            detail="VSS 완료 index commit이 Snapshot target revision과 일치하지 않습니다.",
            retryable=False,
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
