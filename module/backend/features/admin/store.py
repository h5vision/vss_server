"""Admin-only queries over collection, Snapshot, and audit history."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.infrastructure.database.models import (
    AuditLog,
    BranchHeadHistory,
    RepositorySyncRun,
    Snapshot,
    TrackedBranch,
)


class AdminStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_tracked_branches(
        self,
        *,
        repository_id: UUID | None = None,
        tracked: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TrackedBranch]:
        statement = select(TrackedBranch)
        if repository_id is not None:
            statement = statement.where(TrackedBranch.repository_id == repository_id)
        if tracked is not None:
            statement = statement.where(TrackedBranch.tracked.is_(tracked))
        statement = statement.order_by(
            TrackedBranch.repository_id,
            TrackedBranch.branch_ref,
            TrackedBranch.tracked_branch_id,
        ).offset(offset).limit(limit)
        return list(await self._session.scalars(statement))

    async def list_sync_runs(
        self,
        *,
        repository_id: UUID | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RepositorySyncRun]:
        statement = select(RepositorySyncRun)
        if repository_id is not None:
            statement = statement.where(RepositorySyncRun.repository_id == repository_id)
        statement = statement.order_by(
            RepositorySyncRun.started_at.desc(),
            RepositorySyncRun.sync_run_id.desc(),
        ).offset(offset).limit(limit)
        return list(await self._session.scalars(statement))

    async def list_head_history(
        self,
        tracked_branch_id: UUID,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[BranchHeadHistory]:
        statement = (
            select(BranchHeadHistory)
            .where(BranchHeadHistory.tracked_branch_id == tracked_branch_id)
            .order_by(
                BranchHeadHistory.observed_at.desc(),
                BranchHeadHistory.history_id.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
        return list(await self._session.scalars(statement))

    async def list_snapshots(
        self,
        *,
        repository_id: UUID | None = None,
        tracked_branch_id: UUID | None = None,
        state: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Snapshot]:
        statement = select(Snapshot)
        if repository_id is not None:
            statement = statement.where(Snapshot.repository_id == repository_id)
        if tracked_branch_id is not None:
            statement = statement.where(Snapshot.tracked_branch_id == tracked_branch_id)
        if state is not None:
            statement = statement.where(Snapshot.state == state)
        statement = statement.order_by(
            Snapshot.created_at.desc(),
            Snapshot.snapshot_id.desc(),
        ).offset(offset).limit(limit)
        return list(await self._session.scalars(statement))

    async def get_snapshot_detail(self, snapshot_id: UUID) -> Snapshot | None:
        statement = (
            select(Snapshot)
            .where(Snapshot.snapshot_id == snapshot_id)
            .options(selectinload(Snapshot.attempts), selectinload(Snapshot.deltas))
        )
        return await self._session.scalar(statement)

    async def list_audit_logs(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditLog]:
        statement = select(AuditLog).order_by(
            AuditLog.created_at.desc(),
            AuditLog.audit_id.desc(),
        ).offset(offset).limit(limit)
        return list(await self._session.scalars(statement))
