"""Admin의 명시적 요청으로 materialized Snapshot을 VSS 인덱싱에 제출한다."""

from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from backend.core.errors import ApiError
from backend.core.orchestration import MODULE_PUSH, VSS_PULL, IndexOrchestrationMode
from backend.features.indexing.incremental import (
    build_incremental_plan,
    submit_index_with_incremental_fallback,
)
from backend.features.indexing.project_root import IndexProjectRootResolver
from backend.features.materialization.service import SnapshotMaterializer
from backend.features.snapshots.schemas import SnapshotIndexResponse
from backend.features.snapshots.store import SnapshotStore
from backend.infrastructure.database.models import Snapshot, SnapshotAttempt, TrackedBranch
from backend.integrations.vss.client import VssHttpClient
from backend.integrations.vss.errors import VssIntegrationError
from backend.integrations.vss.schemas import (
    VssIndexRequest,
    VssIndexState,
    VssStartIncrementalIndexResponse,
    VssStartIndexResponse,
)
from backend.ports.git import ManagedRepositoryWorkspace, RevisionComparator


@dataclass(frozen=True, slots=True)
class IndexOutcome:
    status_code: int
    body: SnapshotIndexResponse


class SnapshotIndexService:
    """Submit one already-materialized exact Snapshot to VSS on explicit Admin action."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        materializer: SnapshotMaterializer,
        vss_client: VssHttpClient,
        workspace_manager: ManagedRepositoryWorkspace | None = None,
        revision_comparator: RevisionComparator | None = None,
        index_orchestration_mode: IndexOrchestrationMode = MODULE_PUSH,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._vss_client = vss_client
        self._revision_comparator = revision_comparator
        self._project_root_resolver = IndexProjectRootResolver(
            materializer=materializer,
            workspace_manager=workspace_manager,
        )
        self._index_orchestration_mode = index_orchestration_mode

    async def index_tracked_branch(
        self,
        tracked_branch_id: UUID,
        *,
        request_id: UUID,
    ) -> IndexOutcome:
        async with self._sessionmaker() as session:
            try:
                tracked_branch = await session.get(TrackedBranch, tracked_branch_id)
            except SQLAlchemyError as exc:
                raise self._database_unavailable() from exc
            if tracked_branch is None:
                raise ApiError(
                    status_code=404,
                    reason="TRACKED_BRANCH_NOT_FOUND",
                    detail="인덱싱할 Tracked Branch를 찾을 수 없습니다.",
                    retryable=False,
                )
            if not tracked_branch.tracked:
                raise ApiError(
                    status_code=409,
                    reason="TRACKED_BRANCH_INACTIVE",
                    detail="추적이 비활성화된 Branch는 인덱싱을 시작할 수 없습니다.",
                    retryable=False,
                )
            if tracked_branch.current_head_sha is None:
                raise ApiError(
                    status_code=409,
                    reason="TRACKED_BRANCH_HEAD_REQUIRED",
                    detail=(
                        "Branch HEAD가 아직 수집되지 않았습니다. "
                        "Repository를 먼저 Sync해야 합니다."
                    ),
                    retryable=True,
                )
            try:
                snapshot = await SnapshotStore(session).find_by_target(
                    tracked_branch.vss_project_id,
                    tracked_branch.current_head_sha,
                )
            except SQLAlchemyError as exc:
                raise self._database_unavailable() from exc
            if snapshot is None or snapshot.tracked_branch_id != tracked_branch_id:
                raise ApiError(
                    status_code=409,
                    reason="TRACKED_BRANCH_SNAPSHOT_REQUIRED",
                    detail=(
                        "현재 Branch HEAD에 대응하는 검증된 Snapshot이 없습니다. "
                        "Repository를 Sync한 뒤 다시 시도해야 합니다."
                    ),
                    retryable=True,
                )
            snapshot_id = snapshot.snapshot_id

        return await self.index(
            snapshot_id,
            request_id=request_id,
            required_tracked_branch_id=tracked_branch_id,
        )

    async def index(
        self,
        snapshot_id: UUID,
        *,
        request_id: UUID,
        required_tracked_branch_id: UUID | None = None,
    ) -> IndexOutcome:
        if self._index_orchestration_mode == VSS_PULL:
            raise ApiError(
                status_code=409,
                reason="VSS_PULL_OWNS_INDEX_START",
                detail="현재 배포에서는 Module이 VSS 인덱싱 시작 요청을 제출하지 않습니다.",
                retryable=False,
            )

        async with self._sessionmaker() as session:
            store = SnapshotStore(session)
            try:
                snapshot = await store.get_for_update(snapshot_id)
            except SQLAlchemyError as exc:
                raise self._database_unavailable() from exc

            if snapshot is None:
                raise ApiError(
                    status_code=404,
                    reason="SNAPSHOT_NOT_FOUND",
                    detail="인덱싱할 Snapshot을 찾을 수 없습니다.",
                    retryable=False,
                )
            tracked_branch = await self._project_root_resolver.lock_tracked_branch(
                session,
                snapshot,
            )
            if required_tracked_branch_id is not None:
                if snapshot.tracked_branch_id != required_tracked_branch_id:
                    raise ApiError(
                        status_code=409,
                        reason="TRACKED_BRANCH_SNAPSHOT_REQUIRED",
                        detail="선택한 Snapshot이 현재 Tracked Branch에 속하지 않습니다.",
                        retryable=True,
                    )
                if tracked_branch is None:
                    raise ApiError(
                        status_code=404,
                        reason="TRACKED_BRANCH_NOT_FOUND",
                        detail="인덱싱할 Tracked Branch를 찾을 수 없습니다.",
                        retryable=False,
                    )
                if not tracked_branch.tracked:
                    raise ApiError(
                        status_code=409,
                        reason="TRACKED_BRANCH_INACTIVE",
                        detail="추적이 비활성화된 Branch는 인덱싱을 시작할 수 없습니다.",
                        retryable=False,
                    )
                if tracked_branch.current_head_sha != snapshot.target_revision:
                    raise ApiError(
                        status_code=409,
                        reason="TRACKED_BRANCH_SNAPSHOT_REQUIRED",
                        detail=(
                            "Tracked Branch HEAD가 Snapshot 선택 이후 변경되었습니다. "
                            "Repository를 Sync한 뒤 다시 시도해야 합니다."
                        ),
                        retryable=True,
                    )
            if snapshot.state in {"completed", "already_indexed"}:
                return IndexOutcome(
                    status_code=200,
                    body=self._response(
                        snapshot,
                        request_id,
                        reason="TARGET_ALREADY_INDEXED",
                        detail="동일 target revision이 이미 인덱싱되어 다시 제출하지 않았습니다.",
                        retryable=False,
                    ),
                )
            if snapshot.state in {"submitting", "accepted", "indexing"}:
                raise ApiError(
                    status_code=409,
                    reason="VSS_INDEX_ALREADY_RUNNING",
                    detail="이 Snapshot의 인덱싱 제출 또는 처리가 이미 진행 중입니다.",
                    retryable=True,
                    extra=self._snapshot_extra(snapshot),
                )
            if snapshot.state in {"failed", "rejected", "aborted"}:
                raise ApiError(
                    status_code=409,
                    reason="SNAPSHOT_INDEX_RETRY_REQUIRED",
                    detail="실패한 Snapshot은 Retry 작업으로 다시 제출해야 합니다.",
                    retryable=True,
                    extra=self._snapshot_extra(snapshot),
                )
            if snapshot.state != "materialized":
                raise ApiError(
                    status_code=409,
                    reason="SNAPSHOT_INDEX_NOT_ALLOWED",
                    detail="materialized 상태의 Snapshot만 인덱싱을 시작할 수 있습니다.",
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
                    detail="인덱싱 시작 전 VSS 상태를 확인하지 못했습니다.",
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
                    detail="같은 VSS project의 인덱싱이 진행 중이어서 제출하지 않았습니다.",
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
                try:
                    exists = await run_in_threadpool(
                        self._vss_client.exists,
                        snapshot.vss_project_id,
                    )
                except VssIntegrationError as exc:
                    raise ApiError(
                        status_code=503 if exc.retryable else 502,
                        reason=exc.reason,
                        detail="인덱싱 시작 전 VSS active index를 확인하지 못했습니다.",
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
                        "already_indexed",
                        vss_state=status.state.value,
                        vss_reason="TARGET_ALREADY_INDEXED",
                        vss_detail="VSS active index가 Snapshot target revision과 일치합니다.",
                    )
                    await session.commit()
                except SQLAlchemyError as exc:
                    await session.rollback()
                    raise self._result_persist_failed(snapshot) from exc
                return IndexOutcome(
                    status_code=200,
                    body=self._response(
                        snapshot,
                        request_id,
                        reason="TARGET_ALREADY_INDEXED",
                        detail="VSS active index가 target revision과 일치하여 제출하지 않았습니다.",
                        retryable=False,
                    ),
                )

            # VSS가 같은 project_root를 비동기로 읽는 동안 Branch workspace가 바뀌지 않도록
            # running/idempotency 확인을 끝낸 뒤에만 exact Branch working copy를 refresh한다.
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
            incremental_plan = await run_in_threadpool(
                build_incremental_plan,
                revision_comparator=self._revision_comparator,
                repository_id=snapshot.repository_id,
                project_id=snapshot.vss_project_id,
                branch_ref=snapshot.branch_ref,
                project_root=resolved_root.project_root,
                base_revision=active_revision,
                target_revision=snapshot.target_revision,
                profile=full_request.profile,
                briefing=full_request.briefing,
            )

            try:
                await store.set_state(snapshot, "submitting")
                attempt = await store.start_attempt(snapshot, request_id=request_id)
                await session.commit()
            except SQLAlchemyError as exc:
                await session.rollback()
                raise self._result_persist_failed(snapshot) from exc

            started = time.perf_counter()
            planned_mode = "incremental" if incremental_plan.request is not None else "full"
            try:
                submission = await run_in_threadpool(
                    submit_index_with_incremental_fallback,
                    vss_client=self._vss_client,
                    full_request=full_request,
                    incremental_plan=incremental_plan,
                )
            except VssIntegrationError as exc:
                await self._finish_exception(
                    session,
                    store,
                    snapshot,
                    attempt,
                    exc,
                    (time.perf_counter() - started) * 1000,
                    submission_mode=planned_mode,
                )
                raise ApiError(
                    status_code=503 if exc.retryable else 502,
                    reason=exc.reason,
                    detail="VSS 인덱싱 시작 요청을 완료하지 못했습니다.",
                    retryable=exc.retryable,
                    extra=self._snapshot_extra(snapshot),
                ) from exc

            return await self._finish_result(
                session,
                store,
                snapshot,
                attempt,
                submission.upstream,
                (time.perf_counter() - started) * 1000,
                request_id,
                submission_mode=submission.mode,
                planning_fallback_reason=submission.planning_fallback_reason,
                incremental_fallback=submission.incremental_fallback,
            )

    async def _finish_result(
        self,
        session: AsyncSession,
        store: SnapshotStore,
        snapshot: Snapshot,
        attempt: SnapshotAttempt,
        upstream: VssStartIndexResponse | VssStartIncrementalIndexResponse,
        latency_ms: float,
        request_id: UUID,
        *,
        submission_mode: str = "full",
        planning_fallback_reason: str | None = None,
        incremental_fallback: dict[str, object] | None = None,
    ) -> IndexOutcome:
        result = upstream.result
        vss_state = result.state.value if result.state is not None else None
        result_json = {
            "accepted": result.accepted,
            "project_id": result.project_id,
            "state": vss_state,
            "reason": result.reason,
            "heartbeat_age_s": result.heartbeat_age_s,
            "fingerprint": result.fingerprint,
            "submission_mode": submission_mode,
            "planning_fallback_reason": planning_fallback_reason,
            "incremental_fallback": incremental_fallback,
            "full_reindex_required": getattr(result, "full_reindex_required", False),
        }
        if result.accepted:
            state = "accepted"
            reason = "VSS_INDEX_ACCEPTED"
            if submission_mode == "incremental":
                detail = "materialized Snapshot의 VSS 증분 인덱싱 요청이 접수됐습니다."
            elif submission_mode == "full_fallback":
                detail = "증분 사전조건 불일치 후 VSS full 인덱싱 요청이 접수됐습니다."
            else:
                detail = "materialized Snapshot의 VSS 인덱싱 요청이 접수됐습니다."
            retryable = False
            status_code = 202
        elif result.reason == "already_indexed":
            state = "already_indexed"
            reason = "TARGET_ALREADY_INDEXED"
            detail = "VSS가 target revision을 이미 active index로 확인했습니다."
            retryable = False
            status_code = 200
        elif result.reason == "already_running":
            state = "rejected"
            reason = "VSS_INDEX_ALREADY_RUNNING"
            detail = (
                "VSS가 동시 작업 때문에 요청을 접수하지 않았습니다. "
                "Snapshot은 재시도 가능한 거부 상태로 유지합니다."
            )
            retryable = True
            status_code = 409
        elif result.reason == "not_a_directory":
            state = "failed"
            reason = "SNAPSHOT_MATERIALIZATION_FAILED"
            detail = "VSS가 project_root를 디렉터리로 확인하지 못했습니다."
            retryable = True
            status_code = 500
        else:
            state = "rejected"
            reason = "VSS_HTTP_REQUEST_REJECTED"
            detail = "VSS가 Snapshot 인덱싱 요청을 거부했습니다."
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
        return IndexOutcome(
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
        *,
        submission_mode: str = "full",
    ) -> None:
        detail = "VSS 인덱싱 시작 요청을 완료하지 못했습니다."
        try:
            await store.finish_attempt(
                attempt,
                upstream_status_code=exc.upstream_status_code,
                vss_state=None,
                vss_reason=exc.reason,
                vss_detail=detail,
                retryable=exc.retryable,
                latency_ms=latency_ms,
                result_json={"submission_mode": submission_mode},
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
    ) -> SnapshotIndexResponse:
        return SnapshotIndexResponse(
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
            detail="Snapshot 인덱싱 결과를 저장하지 못했습니다.",
            retryable=True,
            extra=cls._snapshot_extra(snapshot),
        )
