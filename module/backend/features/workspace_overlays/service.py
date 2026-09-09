"""Persist, materialize, and submit one Frontend workspace overlay."""

from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from backend.core.errors import ApiError
from backend.core.orchestration import MODULE_PUSH, VSS_PULL, IndexOrchestrationMode
from backend.features.materialization.errors import MaterializationError
from backend.features.materialization.service import SnapshotMaterializer
from backend.features.repositories.store import BranchBindingStore, StoreLookupError
from backend.features.snapshots.store import SnapshotStore
from backend.features.workspace_overlays.mapper import to_vss_index_command
from backend.features.workspace_overlays.schemas import (
    WorkspaceOverlayRequest,
    WorkspaceOverlayResponse,
)
from backend.infrastructure.database.models import Repository, Snapshot
from backend.integrations.vss.client import VssHttpClient
from backend.integrations.vss.errors import VssIntegrationError
from backend.integrations.vss.schemas import VssStartIndexResponse


@dataclass(frozen=True, slots=True)
class OverlayOutcome:
    status_code: int
    body: WorkspaceOverlayResponse


class WorkspaceOverlayService:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        materializer: SnapshotMaterializer,
        vss_client: VssHttpClient,
        index_orchestration_mode: IndexOrchestrationMode = MODULE_PUSH,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._materializer = materializer
        self._vss_client = vss_client
        self._index_orchestration_mode = index_orchestration_mode

    async def execute(
        self,
        request: WorkspaceOverlayRequest,
        *,
        request_id: UUID,
    ) -> OverlayOutcome:
        async with self._sessionmaker() as session:
            try:
                binding = await BranchBindingStore(session).resolve_active(request.project_id)
                repository = await session.get(Repository, binding.repository_id)
            except StoreLookupError as exc:
                raise ApiError(
                    status_code=409,
                    reason=exc.reason,
                    detail=exc.detail,
                    retryable=exc.retryable,
                ) from exc
            except SQLAlchemyError as exc:
                raise self._database_unavailable() from exc

            if repository is None or not repository.active:
                raise ApiError(
                    status_code=409,
                    reason="SNAPSHOT_REPOSITORY_INACTIVE",
                    detail="활성 Snapshot Repository를 찾을 수 없습니다.",
                    retryable=False,
                )

            store = SnapshotStore(session)
            target_revision = request.target_revision.lower()
            try:
                existing = await store.find_by_target(binding.vss_project_id, target_revision)
                if existing is not None:
                    return self._duplicate_outcome(existing, request_id)
                snapshot = await store.create_from_overlay(
                    request,
                    request_id=request_id,
                    binding=binding,
                )
                await session.commit()
            except IntegrityError:
                await session.rollback()
                try:
                    existing = await store.find_by_target(
                        binding.vss_project_id,
                        target_revision,
                    )
                except SQLAlchemyError as exc:
                    raise self._database_unavailable() from exc
                if existing is None:
                    raise self._snapshot_persist_failed() from None
                return self._duplicate_outcome(existing, request_id)
            except SQLAlchemyError as exc:
                await session.rollback()
                raise self._snapshot_persist_failed() from exc

            await self._commit_state(session, store, snapshot, "materializing")
            try:
                materialized = await run_in_threadpool(
                    self._materializer.materialize,
                    request,
                    binding_id=binding.binding_id,
                    snapshot_id=snapshot.snapshot_id,
                    remote_url=repository.remote_url,
                    branch_ref=binding.branch_ref,
                )
            except MaterializationError as exc:
                await self._commit_failure(session, store, snapshot, exc.reason, exc.detail)
                raise ApiError(
                    status_code=exc.status_code,
                    reason=exc.reason,
                    detail=exc.detail,
                    retryable=exc.retryable,
                    extra=self._snapshot_extra(snapshot),
                ) from exc

            await self._commit_state(
                session,
                store,
                snapshot,
                "materialized",
                materialized_locator=materialized.locator,
            )
            snapshot.source_type = materialized.source_type
            if self._index_orchestration_mode == VSS_PULL:
                return OverlayOutcome(
                    status_code=202,
                    body=self._response(
                        snapshot,
                        request_id,
                        ok=True,
                        reason="SNAPSHOT_READY_FOR_VSS_PULL",
                        detail=(
                            "immutable Snapshot이 준비됐습니다. VSS가 내부 source API를 "
                            "pull하여 인덱싱을 시작합니다."
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

            submission = to_vss_index_command(
                request,
                vss_project_id=binding.vss_project_id,
                materialized_project_root=str(materialized.project_root),
                snapshot_id=str(snapshot.snapshot_id),
            )
            started = time.perf_counter()
            try:
                upstream = await run_in_threadpool(
                    self._vss_client.start_index,
                    submission.request,
                )
            except VssIntegrationError as exc:
                latency_ms = (time.perf_counter() - started) * 1000
                await self._finish_vss_failure(
                    session,
                    store,
                    snapshot,
                    attempt,
                    exc,
                    latency_ms,
                )
                raise ApiError(
                    status_code=503 if exc.retryable else 502,
                    reason=exc.reason,
                    detail="VSS 인덱싱 요청을 완료하지 못했습니다.",
                    retryable=exc.retryable,
                    extra=self._snapshot_extra(snapshot),
                ) from exc

            latency_ms = (time.perf_counter() - started) * 1000
            return await self._finish_vss_result(
                session,
                store,
                snapshot,
                attempt,
                upstream,
                latency_ms,
                request_id,
            )

    async def _finish_vss_result(
        self,
        session: AsyncSession,
        store: SnapshotStore,
        snapshot: Snapshot,
        attempt,
        upstream: VssStartIndexResponse,
        latency_ms: float,
        request_id: UUID,
    ) -> OverlayOutcome:
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
            detail = "전체 프로젝트 디렉터리 인덱싱을 접수했습니다. 완료 상태 확인이 필요합니다."
            try:
                await store.finish_attempt(
                    attempt,
                    upstream_status_code=upstream.status_code,
                    vss_state=vss_state,
                    vss_reason="accepted",
                    vss_detail=detail,
                    retryable=False,
                    latency_ms=latency_ms,
                    result_json=result_json,
                )
                await store.set_state(
                    snapshot,
                    "accepted",
                    vss_state=vss_state,
                    vss_reason="accepted",
                    vss_detail=detail,
                )
                await session.commit()
            except SQLAlchemyError as exc:
                await session.rollback()
                raise self._result_persist_failed(snapshot) from exc
            return OverlayOutcome(
                status_code=202,
                body=self._response(
                    snapshot,
                    request_id,
                    ok=True,
                    reason="VSS_INDEX_ACCEPTED",
                    detail=detail,
                    retryable=False,
                ),
            )

        if result.reason == "already_running":
            status_code = 409
            reason = "VSS_INDEX_ALREADY_RUNNING"
            detail = "같은 VSS project의 인덱싱이 진행 중이어서 새 작업을 제출하지 않았습니다."
            retryable = True
            state = "rejected"
        elif result.reason == "not_a_directory":
            status_code = 500
            reason = "SNAPSHOT_MATERIALIZATION_FAILED"
            detail = "VSS가 materialized project_root를 디렉터리로 확인하지 못했습니다."
            retryable = True
            state = "failed"
        else:
            status_code = 502
            reason = "VSS_HTTP_REQUEST_REJECTED"
            detail = "VSS가 인덱싱 요청을 거부했습니다."
            retryable = False
            state = "rejected"

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
        raise ApiError(
            status_code=status_code,
            reason=reason,
            detail=detail,
            retryable=retryable,
            extra={**self._snapshot_extra(snapshot), "vss_reason": result.reason},
        )

    async def _finish_vss_failure(
        self,
        session: AsyncSession,
        store: SnapshotStore,
        snapshot: Snapshot,
        attempt,
        exc: VssIntegrationError,
        latency_ms: float,
    ) -> None:
        detail = "VSS 인덱싱 요청을 완료하지 못했습니다."
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

    async def _commit_state(
        self,
        session: AsyncSession,
        store: SnapshotStore,
        snapshot: Snapshot,
        state: str,
        *,
        materialized_locator: str | None = None,
    ) -> None:
        try:
            await store.set_state(
                snapshot,
                state,
                materialized_locator=materialized_locator,
            )
            await session.commit()
        except SQLAlchemyError as exc:
            await session.rollback()
            raise self._result_persist_failed(snapshot) from exc

    async def _commit_failure(
        self,
        session: AsyncSession,
        store: SnapshotStore,
        snapshot: Snapshot,
        reason: str,
        detail: str,
    ) -> None:
        try:
            await store.set_state(
                snapshot,
                "failed",
                vss_reason=reason,
                vss_detail=detail,
            )
            await session.commit()
        except SQLAlchemyError as exc:
            await session.rollback()
            raise self._result_persist_failed(snapshot) from exc

    @staticmethod
    def _response(
        snapshot: Snapshot,
        request_id: UUID,
        *,
        ok: bool,
        reason: str,
        detail: str,
        retryable: bool,
    ) -> WorkspaceOverlayResponse:
        return WorkspaceOverlayResponse(
            ok=ok,
            reason=reason,
            detail=detail,
            retryable=retryable,
            request_id=request_id,
            snapshot_id=snapshot.snapshot_id,
            project_id=snapshot.vss_project_id,
            state=snapshot.state,
            target_revision=snapshot.target_revision,
            vss_reason=snapshot.vss_reason,
        )

    def _duplicate_outcome(self, snapshot: Snapshot, request_id: UUID) -> OverlayOutcome:
        if snapshot.state in {"completed", "already_indexed"}:
            return OverlayOutcome(
                status_code=200,
                body=self._response(
                    snapshot,
                    request_id,
                    ok=True,
                    reason="TARGET_ALREADY_INDEXED",
                    detail="동일 target revision이 이미 활성 index로 처리되었습니다.",
                    retryable=False,
                ),
            )
        return OverlayOutcome(
            status_code=409,
            body=self._response(
                snapshot,
                request_id,
                ok=False,
                reason="SNAPSHOT_ALREADY_EXISTS",
                detail="동일 target revision의 Snapshot이 이미 존재하여 중복 제출하지 않았습니다.",
                retryable=snapshot.state in {"failed", "rejected", "aborted"},
            ),
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

    @staticmethod
    def _snapshot_persist_failed() -> ApiError:
        return ApiError(
            status_code=500,
            reason="SNAPSHOT_PERSIST_FAILED",
            detail="Snapshot 최초 저장에 실패하여 filesystem과 VSS 작업을 시작하지 않았습니다.",
            retryable=True,
        )

    @classmethod
    def _result_persist_failed(cls, snapshot: Snapshot) -> ApiError:
        return ApiError(
            status_code=500,
            reason="SNAPSHOT_RESULT_PERSIST_FAILED",
            detail="Snapshot 처리 결과를 데이터베이스에 기록하지 못했습니다.",
            retryable=True,
            extra=cls._snapshot_extra(snapshot),
        )
