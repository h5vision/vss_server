"""Snapshot persistence operations used by the overlay ingestion workflow."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.features.workspace_overlays.schemas import WorkspaceOverlayRequest
from backend.infrastructure.database.models import (
    BranchBinding,
    Snapshot,
    SnapshotAttempt,
    SnapshotDelta,
)


class SnapshotStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_target(self, vss_project_id: str, target_revision: str) -> Snapshot | None:
        statement = select(Snapshot).where(
            Snapshot.vss_project_id == vss_project_id,
            Snapshot.target_revision == target_revision,
        )
        return await self._session.scalar(statement)

    async def get(self, snapshot_id: UUID) -> Snapshot | None:
        return await self._session.get(Snapshot, snapshot_id)

    async def get_for_update(self, snapshot_id: UUID) -> Snapshot | None:
        # 동일 Snapshot의 수동 재시도가 동시에 attempt_number를 계산하지 못하도록 실제
        # PostgreSQL에서는 row lock을 잡는다. SQLite에서는 개발용 no-op으로 동작한다.
        statement = (
            select(Snapshot)
            .where(Snapshot.snapshot_id == snapshot_id)
            .with_for_update()
        )
        return await self._session.scalar(statement)

    async def latest_for_binding(self, binding_id: UUID) -> Snapshot | None:
        statement = (
            select(Snapshot)
            .where(Snapshot.binding_id == binding_id)
            .order_by(Snapshot.created_at.desc(), Snapshot.snapshot_id.desc())
            .limit(1)
        )
        return await self._session.scalar(statement)

    async def source_for_vss_project(
        self,
        vss_project_id: str,
        *,
        revision: str | None = None,
    ) -> Snapshot | None:
        statement = select(Snapshot).where(
            Snapshot.vss_project_id == vss_project_id,
            Snapshot.materialized_locator.is_not(None),
        )
        if revision is not None:
            statement = statement.where(Snapshot.target_revision == revision)
        statement = statement.order_by(
            Snapshot.created_at.desc(),
            Snapshot.snapshot_id.desc(),
        ).limit(1)
        return await self._session.scalar(statement)

    async def revisions_for_vss_project(
        self,
        vss_project_id: str,
        *,
        limit: int = 100,
    ) -> list[Snapshot]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        statement = (
            select(Snapshot)
            .where(Snapshot.vss_project_id == vss_project_id)
            .order_by(Snapshot.created_at.desc(), Snapshot.snapshot_id.desc())
            .limit(limit)
        )
        return list(await self._session.scalars(statement))

    async def recovery_candidates(self, *, limit: int = 100) -> list[Snapshot]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        statement = (
            select(Snapshot)
            .where(Snapshot.state.in_(("submitting", "accepted", "indexing")))
            .order_by(Snapshot.updated_at, Snapshot.snapshot_id)
            .limit(limit)
        )
        return list(await self._session.scalars(statement))

    async def list_admin_snapshots(
        self,
        *,
        repository_id: UUID | None = None,
        vss_project_id: str | None = None,
        state: str | None = None,
        limit: int = 100,
    ) -> list[Snapshot]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        statement = select(Snapshot).order_by(
            Snapshot.created_at.desc(), Snapshot.snapshot_id.desc()
        )
        if repository_id is not None:
            statement = statement.where(Snapshot.repository_id == repository_id)
        if vss_project_id is not None:
            statement = statement.where(Snapshot.vss_project_id == vss_project_id.strip())
        if state is not None:
            statement = statement.where(Snapshot.state == state.strip())
        return list(await self._session.scalars(statement.limit(limit)))

    async def get_attempts(self, snapshot_id: UUID) -> list[SnapshotAttempt]:
        statement = (
            select(SnapshotAttempt)
            .where(SnapshotAttempt.snapshot_id == snapshot_id)
            .order_by(SnapshotAttempt.attempt_number.asc())
        )
        return list(await self._session.scalars(statement))

    async def count_deltas_by_status(self, snapshot_id: UUID) -> dict[str, int]:
        statement = select(SnapshotDelta.status).where(
            SnapshotDelta.snapshot_id == snapshot_id
        )
        statuses = list(await self._session.scalars(statement))
        return {
            "changed_file_count": sum(
                1 for s in statuses if s in ("added", "modified", "renamed")
            ),
            "deleted_path_count": sum(1 for s in statuses if s == "deleted"),
            "rename_count": sum(1 for s in statuses if s == "renamed"),
        }

    async def create_from_overlay(
        self,
        request: WorkspaceOverlayRequest,
        *,
        request_id: UUID,
        binding: BranchBinding,
    ) -> Snapshot:
        snapshot = Snapshot(
            request_id=request_id,
            binding_id=binding.binding_id,
            frontend_project_id=request.project_id,
            repository_id=binding.repository_id,
            branch_ref=binding.branch_ref,
            vss_project_id=binding.vss_project_id,
            base_revision=request.base_revision.lower(),
            target_revision=request.target_revision.lower(),
            source_type="remote_clone",
            state="validated",
        )
        self._session.add(snapshot)
        await self._session.flush()

        renames = {item.new_path: item.old_path for item in request.renames}
        for item in request.files:
            old_path = renames.get(item.path)
            self._session.add(
                SnapshotDelta(
                    snapshot_id=snapshot.snapshot_id,
                    status="renamed" if old_path is not None else item.status,
                    path=item.path,
                    old_path=old_path,
                    encoding=item.encoding,
                    content=item.content,
                )
            )
        for path in request.deleted_paths:
            self._session.add(
                SnapshotDelta(
                    snapshot_id=snapshot.snapshot_id,
                    status="deleted",
                    path=path,
                    encoding="utf-8",
                )
            )
        await self._session.flush()
        return snapshot

    async def set_state(
        self,
        snapshot: Snapshot,
        state: str,
        *,
        materialized_locator: str | None = None,
        vss_state: str | None = None,
        vss_reason: str | None = None,
        vss_detail: str | None = None,
    ) -> None:
        snapshot.state = state
        if materialized_locator is not None:
            snapshot.materialized_locator = materialized_locator
        if vss_state is not None:
            snapshot.vss_state = vss_state
        if vss_reason is not None:
            snapshot.vss_reason = vss_reason
        if vss_detail is not None:
            snapshot.vss_detail = vss_detail
        await self._session.flush()

    async def start_attempt(self, snapshot: Snapshot, *, request_id: UUID) -> SnapshotAttempt:
        snapshot.attempt_count += 1
        attempt = SnapshotAttempt(
            snapshot_id=snapshot.snapshot_id,
            request_id=request_id,
            attempt_number=snapshot.attempt_count,
        )
        self._session.add(attempt)
        await self._session.flush()
        return attempt

    async def finish_attempt(
        self,
        attempt: SnapshotAttempt,
        *,
        upstream_status_code: int | None,
        vss_state: str | None,
        vss_reason: str,
        vss_detail: str,
        retryable: bool,
        latency_ms: float,
        result_json: dict | None,
    ) -> None:
        attempt.finished_at = datetime.now(timezone.utc)
        attempt.upstream_status_code = upstream_status_code
        attempt.vss_state = vss_state
        attempt.vss_reason = vss_reason
        attempt.vss_detail = vss_detail
        attempt.retryable = retryable
        attempt.latency_ms = latency_ms
        attempt.vss_result_json = result_json
        await self._session.flush()
