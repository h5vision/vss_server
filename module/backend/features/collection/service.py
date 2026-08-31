"""Repository·Branch 수집 sync 서비스.

수동·정기 동기화가 하나의 흐름을 공유한다. 원격 HEAD를 관측해 append-only 이력으로
보존하고, 새 HEAD마다 중복 없이 Snapshot을 만들어 mirror에서 materialize한 뒤 VSS
`POST /index`에 접수한다. Git credential·remote stderr·mirror 경로는 어떤 응답·로그에도
노출하지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from backend.core.errors import ApiError
from backend.features.collection.errors import CollectionError
from backend.features.collection.git_client import GitCollectionClient
from backend.features.collection.materializer import CollectionMaterializer
from backend.features.materialization.errors import MaterializationError
from backend.features.snapshots.store import SnapshotStore
from backend.infrastructure.database.models import (
    BranchHeadHistory,
    Repository,
    RepositorySyncRun,
    Snapshot,
    TrackedBranch,
)
from backend.integrations.vss.client import VssHttpClient
from backend.integrations.vss.errors import VssIntegrationError
from backend.integrations.vss.schemas import VssIndexRequest, VssStartIndexResponse

logger = logging.getLogger(__name__)

CHANGE_INITIAL = "initial"
CHANGE_FAST_FORWARD = "fast_forward"
CHANGE_REWIND = "rewind"
CHANGE_DELETED = "branch_deleted"

RUN_RUNNING = "running"
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SyncRunSummary:
    sync_run_id: UUID
    repository_id: UUID
    state: str
    reason: str | None
    detail: str | None
    observed_branches: int
    changed_branches: int
    snapshots_created: int
    snapshots_accepted: int
    snapshot_failures: int


@dataclass(frozen=True, slots=True)
class _Observation:
    change_type: str
    previous_head_sha: str | None
    observed_head_sha: str | None


@dataclass(slots=True)
class _RunContext:
    sync_run_id: UUID
    repository_id: UUID
    mirror_dir: Path
    outcomes: list[tuple[str, str]] = field(default_factory=list)
    observed: int = 0
    changed: int = 0
    created: int = 0
    accepted: int = 0
    failures: int = 0


class RepositoryCollectionService:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        git: GitCollectionClient,
        materializer: CollectionMaterializer,
        vss_client: VssHttpClient,
        collection_root: Path,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._git = git
        self._materializer = materializer
        self._vss_client = vss_client
        self._collection_root = collection_root.expanduser().resolve()
        self._locks: dict[UUID, asyncio.Lock] = {}

    # ------------------------------------------------------------------ sync

    async def sync_repository(
        self,
        repository_id: UUID,
        *,
        trigger: str = "manual",
    ) -> SyncRunSummary:
        """한 Repository의 수집 동기화를 실행한다.

        같은 프로세스의 동시 호출은 Repository별 asyncio lock으로 직렬화하고, 프로세스
        간 경쟁은 repository_sync_runs의 running 부분 유니크 인덱스가 차단한다.
        """
        lock = self._locks.setdefault(repository_id, asyncio.Lock())
        async with lock:
            return await self._sync_locked(repository_id, trigger)

    async def sync_all(self, *, trigger: str, limit: int = 100) -> list[SyncRunSummary]:
        """활성 Repository 전체를 순서대로 동기화한다. 개별 실패는 전체를 중단하지 않는다."""
        async with self._sessionmaker() as session:
            result = await session.scalars(
                select(Repository)
                .where(Repository.active.is_(True))
                .order_by(Repository.created_at, Repository.repository_id)
                .limit(limit)
            )
            repository_ids = [row.repository_id for row in result]
        summaries: list[SyncRunSummary] = []
        for repository_id in repository_ids:
            try:
                summaries.append(await self.sync_repository(repository_id, trigger=trigger))
            except ApiError as exc:
                logger.warning(
                    "collection_sync_skipped repository_id=%s reason=%s",
                    repository_id,
                    exc.reason,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("collection_sync_unexpected repository_id=%s", repository_id)
        return summaries

    async def _sync_locked(self, repository_id: UUID, trigger: str) -> SyncRunSummary:
        async with self._sessionmaker() as session:
            try:
                repository = await self._require_repository(session, repository_id)
                context = await self._open_run(session, repository, trigger)
            except SQLAlchemyError as exc:
                raise self._database_unavailable() from exc

            try:
                await self._observe_heads(session, repository, context)
                await self._materialize_and_submit(session, repository, context)
                await self._finish_run(session, context)
            except CollectionError as exc:
                await self._fail_run(session, context, exc.reason, exc.detail)
                return self._summary(context, exc.reason, exc.detail)
            except SQLAlchemyError as exc:
                await self._fail_run(
                    session,
                    context,
                    "DATABASE_UNAVAILABLE",
                    "수집 결과를 데이터베이스에 기록하지 못했습니다.",
                )
                raise self._database_unavailable() from exc

            return self._summary(context, None, self._run_detail(context))

    async def _observe_heads(
        self,
        session: AsyncSession,
        repository: Repository,
        context: _RunContext,
    ) -> None:
        heads = await run_in_threadpool(self._git.remote_heads, repository.remote_url)
        await run_in_threadpool(
            self._git.ensure_mirror,
            repository.remote_url,
            context.mirror_dir,
        )

        existing_branches = list(
            await session.scalars(
                select(TrackedBranch).where(
                    TrackedBranch.repository_id == repository.repository_id
                )
            )
        )
        branch_map: dict[str, TrackedBranch] = {b.branch_ref: b for b in existing_branches}

        # 원격 Git에서 발견된 모든 브랜치를 자동 추적 대상(TrackedBranch)으로 등록 (Auto-Discovery)
        for remote_ref in sorted(heads.keys()):
            if remote_ref not in branch_map:
                short_name = remote_ref.removeprefix("refs/heads/").replace("/", "-")
                vss_proj = f"{repository.canonical_name}-{short_name}"
                new_branch = TrackedBranch(
                    repository_id=repository.repository_id,
                    branch_ref=remote_ref,
                    vss_project_id=vss_proj,
                    tracked=True,
                )
                session.add(new_branch)
                branch_map[remote_ref] = new_branch

        await session.flush()

        active_branches = [b for b in branch_map.values() if b.tracked]
        active_branches.sort(key=lambda b: b.branch_ref)

        now = datetime.now(timezone.utc)
        for branch in active_branches:
            context.observed += 1
            observed_sha = heads.get(branch.branch_ref)
            previous = branch.current_head_sha.lower() if branch.current_head_sha else None
            if observed_sha is not None and previous is not None and previous != observed_sha:
                # fast-forward/rewind 분류는 Git merge-base 조회가 필요하므로 blocking
                # 호출은 threadpool로 돌린다.
                is_ancestor = await run_in_threadpool(
                    self._git.is_ancestor,
                    context.mirror_dir,
                    previous,
                    observed_sha,
                )
            else:
                is_ancestor = False
            observation = _classify(previous, observed_sha, is_ancestor)
            if observation is None:
                branch.last_fetched_at = now
                continue
            context.changed += 1
            session.add(
                BranchHeadHistory(
                    tracked_branch_id=branch.tracked_branch_id,
                    previous_head_sha=observation.previous_head_sha,
                    observed_head_sha=observation.observed_head_sha,
                    change_type=observation.change_type,
                    sync_run_id=context.sync_run_id,
                )
            )
            branch.current_head_sha = observation.observed_head_sha
            branch.last_fetched_at = now
            observed = observation.observed_head_sha or "deleted"
            change_summary = f"{observation.change_type}:{observed}"
            context.outcomes.append((branch.branch_ref, change_summary))
            await self._queue_snapshot(session, branch, observation, context)
        await session.commit()

    async def _queue_snapshot(
        self,
        session: AsyncSession,
        branch: TrackedBranch,
        observation: _Observation,
        context: _RunContext,
    ) -> None:
        """새 HEAD마다 중복 없이 Snapshot을 준비한다.

        동일 HEAD 재수집은 새 Snapshot·VSS Job을 만들지 않는다. force-push로 브랜치가
        다시 가리킨 과거 SHA도 이미 Snapshot이 있으면 재제출하지 않는다.
        """
        if observation.observed_head_sha is None:
            return
        existing = await SnapshotStore(session).find_by_target(
            branch.vss_project_id,
            observation.observed_head_sha,
        )
        if existing is not None:
            return
        session.add(
            Snapshot(
                request_id=context.sync_run_id,
                binding_id=None,
                tracked_branch_id=branch.tracked_branch_id,
                frontend_project_id=None,
                repository_id=branch.repository_id,
                branch_ref=branch.branch_ref,
                vss_project_id=branch.vss_project_id,
                base_revision=observation.observed_head_sha,
                target_revision=observation.observed_head_sha,
                source_type="remote_clone",
                state="validated",
            )
        )
        context.created += 1

    async def _materialize_and_submit(
        self,
        session: AsyncSession,
        repository: Repository,
        context: _RunContext,
    ) -> None:
        store = SnapshotStore(session)
        pending = list(
            await session.scalars(
                select(Snapshot)
                .where(
                    Snapshot.request_id == context.sync_run_id,
                    Snapshot.state == "validated",
                )
                .order_by(Snapshot.created_at, Snapshot.snapshot_id)
            )
        )
        for snapshot in pending:
            await self._process_snapshot(session, store, repository, context, snapshot)

    async def _process_snapshot(
        self,
        session: AsyncSession,
        store: SnapshotStore,
        repository: Repository,
        context: _RunContext,
        snapshot: Snapshot,
    ) -> None:
        await store.set_state(snapshot, "materializing")
        await session.commit()
        try:
            materialized = await run_in_threadpool(
                self._materializer.materialize,
                owner_id=snapshot.tracked_branch_id,
                snapshot_id=snapshot.snapshot_id,
                mirror_dir=context.mirror_dir,
                revision=snapshot.target_revision,
            )
        except MaterializationError as exc:
            context.failures += 1
            context.outcomes.append((snapshot.target_revision, exc.reason))
            await self._commit_snapshot_failure(store, session, snapshot, exc.reason, exc.detail)
            return

        await store.set_state(snapshot, "materialized", materialized_locator=materialized.locator)
        await store.set_state(snapshot, "submitting")
        attempt = await store.start_attempt(snapshot, request_id=context.sync_run_id)
        await session.commit()

        submission = VssIndexRequest(
            project_root=str(materialized.project_root),
            project_id=snapshot.vss_project_id,
            briefing=True,
            note=f"snapshot {snapshot.target_revision}",
        )
        started = time.perf_counter()
        try:
            upstream = await run_in_threadpool(self._vss_client.start_index, submission)
        except VssIntegrationError as exc:
            latency_ms = (time.perf_counter() - started) * 1000
            context.failures += 1
            context.outcomes.append((snapshot.target_revision, exc.reason))
            await self._commit_vss_failure(session, store, snapshot, attempt, exc, latency_ms)
            return

        latency_ms = (time.perf_counter() - started) * 1000
        await self._commit_vss_result(
            session, store, snapshot, attempt, upstream, latency_ms, context
        )

    async def _commit_vss_result(
        self,
        session: AsyncSession,
        store: SnapshotStore,
        snapshot: Snapshot,
        attempt,
        upstream: VssStartIndexResponse,
        latency_ms: float,
        context: _RunContext,
    ) -> None:
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
            detail = "수집 HEAD 전체 디렉터리 인덱싱을 접수했습니다. 완료 상태 확인이 필요합니다."
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
            context.accepted += 1
            context.outcomes.append((snapshot.target_revision, "VSS_INDEX_ACCEPTED"))
            return

        reason = result.reason or "VSS_HTTP_REQUEST_REJECTED"
        state = "rejected" if reason != "not_a_directory" else "failed"
        context.failures += 1
        context.outcomes.append((snapshot.target_revision, reason))
        await store.finish_attempt(
            attempt,
            upstream_status_code=upstream.status_code,
            vss_state=vss_state,
            vss_reason=reason,
            vss_detail="VSS가 수집 Snapshot 인덱싱을 접수하지 않았습니다.",
            retryable=reason == "already_running",
            latency_ms=latency_ms,
            result_json=result_json,
        )
        await store.set_state(snapshot, state, vss_state=vss_state, vss_reason=reason)
        await session.commit()

    async def _commit_vss_failure(
        self,
        session: AsyncSession,
        store: SnapshotStore,
        snapshot: Snapshot,
        attempt,
        exc: VssIntegrationError,
        latency_ms: float,
    ) -> None:
        await store.finish_attempt(
            attempt,
            upstream_status_code=exc.upstream_status_code,
            vss_state=None,
            vss_reason=exc.reason,
            vss_detail="VSS 인덱싱 요청을 완료하지 못했습니다.",
            retryable=exc.retryable,
            latency_ms=latency_ms,
            result_json=None,
        )
        await store.set_state(snapshot, "failed", vss_reason=exc.reason)
        await session.commit()

    async def _commit_snapshot_failure(
        self,
        store: SnapshotStore,
        session: AsyncSession,
        snapshot: Snapshot,
        reason: str,
        detail: str,
    ) -> None:
        await store.set_state(snapshot, "failed", vss_reason=reason, vss_detail=detail)
        await session.commit()

    # ------------------------------------------------------- track / untrack

    async def track_branch(
        self,
        repository_id: UUID,
        *,
        branch_ref: str,
        vss_project_id: str,
    ) -> TrackedBranch:
        """원격 catalog에서 브랜치 존재를 확인한 뒤 exact ref로 추적을 시작한다."""
        normalized_ref = _validate_branch_ref(branch_ref)
        normalized_project = vss_project_id.strip()
        if not normalized_project or len(normalized_project) > 255:
            raise ApiError(
                status_code=422,
                reason="REQUEST_VALIDATION_FAILED",
                detail="vss_project_id는 1~255자여야 합니다.",
                retryable=False,
            )
        async with self._sessionmaker() as session:
            try:
                repository = await self._require_repository(session, repository_id)
            except SQLAlchemyError as exc:
                raise self._database_unavailable() from exc
            if not repository.active:
                raise ApiError(
                    status_code=409,
                    reason="COLLECTION_REPOSITORY_INACTIVE",
                    detail="비활성 Repository의 브랜치는 추적할 수 없습니다.",
                    retryable=False,
                )
            heads = await run_in_threadpool(self._git.remote_heads, repository.remote_url)
            if normalized_ref not in heads:
                raise ApiError(
                    status_code=409,
                    reason="COLLECTION_BRANCH_NOT_FOUND",
                    detail="원격 Repository에서 해당 브랜치를 찾을 수 없습니다.",
                    retryable=False,
                )
            existing = await session.scalar(
                select(TrackedBranch).where(
                    TrackedBranch.repository_id == repository_id,
                    TrackedBranch.branch_ref == normalized_ref,
                )
            )
            if existing is not None:
                raise ApiError(
                    status_code=409,
                    reason="COLLECTION_BRANCH_ALREADY_TRACKED",
                    detail="이미 추적 중이거나 이력이 남아 있는 브랜치입니다.",
                    retryable=False,
                )
            branch = TrackedBranch(
                repository_id=repository_id,
                branch_ref=normalized_ref,
                vss_project_id=normalized_project,
                tracked=True,
            )
            session.add(branch)
            try:
                await session.commit()
            except IntegrityError as exc:
                raise ApiError(
                    status_code=409,
                    reason="COLLECTION_BRANCH_ALREADY_TRACKED",
                    detail="이미 추적 중인 브랜치입니다.",
                    retryable=False,
                ) from exc
            await session.refresh(branch)
            return branch

    async def untrack_branch(self, repository_id: UUID, branch_ref: str) -> TrackedBranch:
        """추적을 해제한다. HEAD 이력과 그동안의 Snapshot은 보존된다."""
        normalized_ref = _validate_branch_ref(branch_ref)
        async with self._sessionmaker() as session:
            branch = await session.scalar(
                select(TrackedBranch).where(
                    TrackedBranch.repository_id == repository_id,
                    TrackedBranch.branch_ref == normalized_ref,
                )
            )
            if branch is None:
                raise ApiError(
                    status_code=404,
                    reason="COLLECTION_BRANCH_NOT_FOUND",
                    detail="추적 기록이 없는 브랜치입니다.",
                    retryable=False,
                )
            branch.tracked = False
            await session.commit()
            await session.refresh(branch)
            return branch

    async def untrack_by_id(self, tracked_branch_id: UUID) -> TrackedBranch:
        async with self._sessionmaker() as session:
            branch = await session.get(TrackedBranch, tracked_branch_id)
            if branch is None:
                raise ApiError(
                    status_code=404,
                    reason="COLLECTION_BRANCH_NOT_FOUND",
                    detail="추적 기록이 없는 브랜치입니다.",
                    retryable=False,
                )
            branch.tracked = False
            await session.commit()
            await session.refresh(branch)
            return branch

    async def history(
        self,
        tracked_branch_id: UUID,
        *,
        limit: int = 100,
    ) -> tuple[TrackedBranch, list[BranchHeadHistory]]:
        """추적 브랜치와 append-only HEAD 이력을 함께 반환한다."""
        async with self._sessionmaker() as session:
            branch = await session.get(TrackedBranch, tracked_branch_id)
            if branch is None:
                raise ApiError(
                    status_code=404,
                    reason="COLLECTION_BRANCH_NOT_FOUND",
                    detail="추적 기록이 없는 브랜치입니다.",
                    retryable=False,
                )
            entries = list(
                await session.scalars(
                    select(BranchHeadHistory)
                    .where(BranchHeadHistory.tracked_branch_id == tracked_branch_id)
                    .order_by(BranchHeadHistory.observed_at.desc(), BranchHeadHistory.history_id)
                    .limit(limit)
                )
            )
            return branch, entries

    async def list_branches(self, repository_id: UUID) -> list[TrackedBranch]:
        async with self._sessionmaker() as session:
            result = await session.scalars(
                select(TrackedBranch)
                .where(TrackedBranch.repository_id == repository_id)
                .order_by(TrackedBranch.branch_ref)
            )
            return list(result)

    async def list_sync_runs(self, repository_id: UUID, *, limit: int = 20) -> list:
        async with self._sessionmaker() as session:
            result = await session.scalars(
                select(RepositorySyncRun)
                .where(RepositorySyncRun.repository_id == repository_id)
                .order_by(RepositorySyncRun.started_at.desc(), RepositorySyncRun.sync_run_id)
                .limit(limit)
            )
            return list(result)

    async def catalog(self, repository_id: UUID) -> dict[str, str]:
        """원격 브랜치 catalog를 exact ref → commit SHA로 조회한다."""
        async with self._sessionmaker() as session:
            repository = await self._require_repository(session, repository_id)
            return await run_in_threadpool(self._git.remote_heads, repository.remote_url)

    # --------------------------------------------------------------- helpers

    async def _require_repository(
        self,
        session: AsyncSession,
        repository_id: UUID,
    ) -> Repository:
        repository = await session.get(Repository, repository_id)
        if repository is None:
            raise ApiError(
                status_code=404,
                reason="COLLECTION_REPOSITORY_NOT_FOUND",
                detail="수집할 Repository를 찾을 수 없습니다.",
                retryable=False,
            )
        return repository

    async def _open_run(
        self,
        session: AsyncSession,
        repository: Repository,
        trigger: str,
    ) -> _RunContext:
        run = RepositorySyncRun(
            repository_id=repository.repository_id,
            trigger=trigger,
            state=RUN_RUNNING,
        )
        session.add(run)
        try:
            await session.commit()
        except IntegrityError as exc:
            raise ApiError(
                status_code=409,
                reason="COLLECTION_SYNC_ALREADY_RUNNING",
                detail="같은 Repository의 수집 동기화가 이미 진행 중입니다.",
                retryable=True,
            ) from exc
        await session.refresh(run)
        return _RunContext(
            sync_run_id=run.sync_run_id,
            repository_id=repository.repository_id,
            mirror_dir=self._mirror_dir(repository.repository_id),
        )

    async def _finish_run(self, session: AsyncSession, context: _RunContext) -> None:
        run = await session.get(RepositorySyncRun, context.sync_run_id)
        if run is not None:
            run.state = RUN_SUCCEEDED
            run.detail = self._run_detail(context)
            run.finished_at = datetime.now(timezone.utc)
        await session.commit()

    async def _fail_run(
        self,
        session: AsyncSession,
        context: _RunContext,
        reason: str,
        detail: str,
    ) -> None:
        try:
            run = await session.get(RepositorySyncRun, context.sync_run_id)
            if run is not None:
                run.state = RUN_FAILED
                run.reason = reason
                run.detail = detail
                run.finished_at = datetime.now(timezone.utc)
                await session.commit()
        except SQLAlchemyError:
            await session.rollback()
            logger.error("collection_run_finalize_failed sync_run_id=%s", context.sync_run_id)

    def _run_detail(self, context: _RunContext) -> str:
        outcomes = ", ".join(f"{ref}={outcome}" for ref, outcome in context.outcomes[:10])
        return (
            f"observed={context.observed} changed={context.changed} "
            f"snapshots_created={context.created} snapshots_accepted={context.accepted} "
            f"snapshot_failures={context.failures} outcomes=[{outcomes}]"
        )

    def _summary(
        self,
        context: _RunContext,
        reason: str | None,
        detail: str | None,
    ) -> SyncRunSummary:
        return SyncRunSummary(
            sync_run_id=context.sync_run_id,
            repository_id=context.repository_id,
            state=RUN_FAILED if reason is not None else RUN_SUCCEEDED,
            reason=reason,
            detail=detail,
            observed_branches=context.observed,
            changed_branches=context.changed,
            snapshots_created=context.created,
            snapshots_accepted=context.accepted,
            snapshot_failures=context.failures,
        )

    def _mirror_dir(self, repository_id: UUID) -> Path:
        candidate = self._collection_root / repository_id.hex / "mirror.git"
        try:
            candidate.resolve().relative_to(self._collection_root)
        except ValueError as exc:
            raise CollectionError(
                reason="COLLECTION_MIRROR_UNAVAILABLE",
                detail="mirror 경로가 전용 collection root 내부에 있지 않습니다.",
                retryable=False,
            ) from exc
        return candidate

    @staticmethod
    def _database_unavailable() -> ApiError:
        return ApiError(
            status_code=503,
            reason="DATABASE_UNAVAILABLE",
            detail="Snapshot 데이터베이스를 사용할 수 없습니다.",
            retryable=True,
        )


def _classify(
    previous_head_sha: str | None,
    observed_head_sha: str | None,
    is_ancestor: bool,
) -> _Observation | None:
    """관측 결과를 change_type으로 분류한다. 변경이 없으면 None을 돌려준다."""
    if observed_head_sha is None:
        if previous_head_sha is None:
            # 한 번도 관측된 적 없는 브랜치의 부재는 기록할 증거가 없다.
            return None
        return _Observation(CHANGE_DELETED, previous_head_sha, None)
    observed = observed_head_sha.lower()
    if previous_head_sha is None:
        return _Observation(CHANGE_INITIAL, None, observed)
    if previous_head_sha == observed:
        return None
    if is_ancestor:
        return _Observation(CHANGE_FAST_FORWARD, previous_head_sha, observed)
    return _Observation(CHANGE_REWIND, previous_head_sha, observed)


def _validate_branch_ref(branch_ref: str) -> str:
    """exact `refs/heads/...` ref만 허용한다. Git ref 규칙 위반은 fail closed."""
    normalized = branch_ref.strip()
    prefix = "refs/heads/"
    rest = normalized[len(prefix):] if normalized.startswith(prefix) else ""
    invalid = (
        not rest
        or rest.startswith("/")
        or rest.endswith("/")
        or rest.endswith(".")
        or rest.endswith(".lock")
        or "//" in normalized
        or ".." in normalized
        or "@{" in normalized
        or any(character.isspace() or ord(character) < 0x20 for character in normalized)
        or any(character in "~^:?*[\\" for character in normalized)
    )
    if invalid:
        raise ApiError(
            status_code=422,
            reason="REQUEST_VALIDATION_FAILED",
            detail="branch_ref는 refs/heads/로 시작하는 유효한 Git ref여야 합니다.",
            retryable=False,
        )
    return normalized
