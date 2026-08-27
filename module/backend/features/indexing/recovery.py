"""One-shot idempotent recovery for non-terminal Snapshot states."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.errors import ApiError
from backend.features.indexing.schemas import RecoverySummary
from backend.features.indexing.service import IndexStatusService
from backend.features.snapshots.store import SnapshotStore
from backend.integrations.vss.client import VssHttpClient


class SnapshotRecoveryCoordinator:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        vss_client: VssHttpClient,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._status_service = IndexStatusService(
            sessionmaker=sessionmaker,
            vss_client=vss_client,
        )

    async def run_once(self, *, limit: int = 100) -> RecoverySummary:
        async with self._sessionmaker() as session:
            try:
                candidates = await SnapshotStore(session).recovery_candidates(limit=limit)
                snapshot_ids = [candidate.snapshot_id for candidate in candidates]
            except SQLAlchemyError:
                return RecoverySummary(examined=0, synchronized=0, unavailable=0, failed=1)

        synchronized = 0
        unavailable = 0
        failed = 0
        for snapshot_id in snapshot_ids:
            try:
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
            examined=len(snapshot_ids),
            synchronized=synchronized,
            unavailable=unavailable,
            failed=failed,
        )
