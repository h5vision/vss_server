"""종료되지 않은 Snapshot을 한 번 멱등 동기화하는 재시작 복구."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.core.errors import ApiError
from backend.features.indexing.recovery_lock import RecoveryRunLock
from backend.features.indexing.schemas import RecoverySummary
from backend.features.indexing.service import IndexStatusService
from backend.features.snapshots.store import SnapshotStore
from backend.integrations.vss.client import VssHttpClient


class SnapshotRecoveryCoordinator:
    def __init__(
        self,
        *,
        engine: AsyncEngine,
        sessionmaker: async_sessionmaker[AsyncSession],
        vss_client: VssHttpClient,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._run_lock = RecoveryRunLock(engine)
        self._status_service = IndexStatusService(
            sessionmaker=sessionmaker,
            vss_client=vss_client,
        )

    async def run_once(self, *, limit: int = 100) -> RecoverySummary:
        try:
            async with self._run_lock.acquire() as lock_acquired:
                if not lock_acquired:
                    # 다른 worker가 같은 DB의 복구를 수행 중이면 중복 VSS 조회 없이 종료한다.
                    return RecoverySummary(
                        lock_acquired=False,
                        examined=0,
                        synchronized=0,
                        unavailable=0,
                        failed=0,
                    )
                return await self._run_locked(limit=limit)
        except SQLAlchemyError:
            return RecoverySummary(
                lock_acquired=False,
                examined=0,
                synchronized=0,
                unavailable=0,
                failed=1,
            )

    async def _run_locked(self, *, limit: int) -> RecoverySummary:
        async with self._sessionmaker() as session:
            try:
                candidates = await SnapshotStore(session).recovery_candidates(limit=limit)
                # VSS 네트워크 조회 동안 DB transaction을 붙잡지 않기 위해 ID만 복사한 뒤
                # 각 Snapshot을 독립 session으로 동기화한다.
                snapshot_ids = [candidate.snapshot_id for candidate in candidates]
            except SQLAlchemyError:
                return RecoverySummary(
                    lock_acquired=True,
                    examined=0,
                    synchronized=0,
                    unavailable=0,
                    failed=1,
                )

        synchronized = 0
        unavailable = 0
        failed = 0
        for snapshot_id in snapshot_ids:
            try:
                # 복구는 상태 조회만 수행한다. 이전 요청이 VSS에서 살아 있을 수 있으므로
                # process restart만을 근거로 자동 재제출하거나 force=true를 사용하지 않는다.
                await self._status_service.synchronize_by_id(
                    snapshot_id,
                    request_id=uuid4(),
                )
                synchronized += 1
            except ApiError as exc:
                if exc.status_code in {502, 503}:
                    unavailable += 1
                else:
                    failed += 1
        return RecoverySummary(
            lock_acquired=True,
            examined=len(snapshot_ids),
            synchronized=synchronized,
            unavailable=unavailable,
            failed=failed,
        )
