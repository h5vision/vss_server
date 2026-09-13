"""Commit graph metadata, parent edges and catalog run persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.features.commit_catalog.errors import CommitCatalogError
from backend.features.commit_catalog.schemas import CommitGraphScanResult
from backend.infrastructure.database.models import (
    CommitCatalogRun,
    Repository,
    RepositoryCommit,
    RepositoryCommitParent,
)


class CommitCatalogStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_run(
        self,
        repository_id: UUID,
        *,
        request_id: UUID,
        roots: list[str],
        max_commits: int,
        lease_seconds: int,
    ) -> tuple[Repository, CommitCatalogRun]:
        now = datetime.now(timezone.utc)
        repository = await self._session.scalar(
            select(Repository)
            .where(Repository.repository_id == repository_id)
            .with_for_update()
        )
        if repository is None:
            raise CommitCatalogError(
                reason="REPOSITORY_NOT_FOUND",
                detail="Commit catalog를 만들 Repository를 찾을 수 없습니다.",
                retryable=False,
                status_code=404,
            )
        if not repository.active:
            raise CommitCatalogError(
                reason="REPOSITORY_INACTIVE",
                detail="비활성 Repository의 Commit catalog는 갱신할 수 없습니다.",
                retryable=False,
                status_code=409,
            )
        active = await self._session.scalar(
            select(CommitCatalogRun).where(
                CommitCatalogRun.repository_id == repository_id,
                CommitCatalogRun.state == "running",
            )
        )
        if active is not None:
            lease_expires_at = active.lease_expires_at
            if lease_expires_at.tzinfo is None:
                lease_expires_at = lease_expires_at.replace(tzinfo=timezone.utc)
            if lease_expires_at > now:
                raise CommitCatalogError(
                    reason="COMMIT_CATALOG_ALREADY_RUNNING",
                    detail="같은 Repository의 Commit catalog 작업이 이미 진행 중입니다.",
                    retryable=True,
                    status_code=409,
                )
            active.state = "failed"
            active.reason = "COMMIT_CATALOG_LEASE_EXPIRED"
            active.detail = "이전 Commit catalog lease가 만료되어 실패로 종료했습니다."
            active.retryable = True
            active.finished_at = now

        run = CommitCatalogRun(
            request_id=request_id,
            repository_id=repository_id,
            state="running",
            reason="COMMIT_CATALOG_RUNNING",
            detail="검증된 Git object에서 Repository commit graph를 수집하고 있습니다.",
            retryable=False,
            roots_json=roots,
            unavailable_roots_json=[],
            max_commits=max_commits,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
        )
        self._session.add(run)
        await self._session.flush()
        return repository, run

    async def persist_scan(
        self,
        repository_id: UUID,
        scan: CommitGraphScanResult,
        *,
        batch_size: int,
        observed_at: datetime,
    ) -> int:
        commits_by_sha: dict[str, RepositoryCommit] = {}
        for start in range(0, len(scan.entries), batch_size):
            batch = scan.entries[start : start + batch_size]
            shas = [entry.commit_sha for entry in batch]
            existing = {
                item.commit_sha: item
                for item in await self._session.scalars(
                    select(RepositoryCommit).where(
                        RepositoryCommit.repository_id == repository_id,
                        RepositoryCommit.commit_sha.in_(shas),
                    )
                )
            }
            for entry in batch:
                commit = existing.get(entry.commit_sha)
                if commit is None:
                    commit = RepositoryCommit(
                        repository_id=repository_id,
                        commit_sha=entry.commit_sha,
                        tree_sha=entry.tree_sha,
                        author_name=entry.author_name,
                        authored_at=entry.authored_at,
                        committed_at=entry.committed_at,
                        subject=entry.subject,
                        object_verified_at=observed_at,
                        last_seen_at=observed_at,
                    )
                    self._session.add(commit)
                else:
                    commit.tree_sha = entry.tree_sha
                    commit.author_name = entry.author_name
                    commit.authored_at = entry.authored_at
                    commit.committed_at = entry.committed_at
                    commit.subject = entry.subject
                    commit.object_verified_at = observed_at
                    commit.last_seen_at = observed_at
                commits_by_sha[entry.commit_sha] = commit
            await self._session.flush()

        all_shas = set(commits_by_sha)
        parent_shas = {
            parent_sha for entry in scan.entries for parent_sha in entry.parent_shas
        }
        unresolved = parent_shas - all_shas
        for start in range(0, len(unresolved), batch_size):
            batch = list(unresolved)[start : start + batch_size]
            for commit in await self._session.scalars(
                select(RepositoryCommit).where(
                    RepositoryCommit.repository_id == repository_id,
                    RepositoryCommit.commit_sha.in_(batch),
                )
            ):
                commits_by_sha[commit.commit_sha] = commit

        child_ids = [commit.repository_commit_id for commit in commits_by_sha.values()]
        existing_edges: dict[tuple[UUID, int], RepositoryCommitParent] = {}
        for start in range(0, len(child_ids), batch_size):
            batch = child_ids[start : start + batch_size]
            for edge in await self._session.scalars(
                select(RepositoryCommitParent).where(
                    RepositoryCommitParent.repository_commit_id.in_(batch)
                )
            ):
                existing_edges[(edge.repository_commit_id, edge.parent_order)] = edge

        missing_reason = (
            "scan_truncated"
            if scan.truncated
            else "shallow_history"
            if scan.shallow
            else "object_unavailable"
        )
        for entry in scan.entries:
            child = commits_by_sha[entry.commit_sha]
            for parent_order, parent_sha in enumerate(entry.parent_shas):
                parent = commits_by_sha.get(parent_sha)
                edge = existing_edges.get((child.repository_commit_id, parent_order))
                if edge is None:
                    edge = RepositoryCommitParent(
                        repository_commit_id=child.repository_commit_id,
                        parent_sha=parent_sha,
                        parent_order=parent_order,
                    )
                    self._session.add(edge)
                edge.parent_sha = parent_sha
                edge.parent_commit_id = (
                    parent.repository_commit_id if parent is not None else None
                )
                edge.parent_missing_reason = None if parent is not None else missing_reason
        await self._session.flush()
        return len(scan.entries)

    async def finish_run(
        self,
        run_id: UUID,
        *,
        state: str,
        reason: str,
        detail: str,
        retryable: bool,
        unavailable_roots: list[str],
        discovered_count: int,
        persisted_count: int,
        truncated: bool,
        shallow: bool,
        history_complete: bool,
        finished_at: datetime,
    ) -> CommitCatalogRun:
        run = await self._session.get(CommitCatalogRun, run_id)
        if run is None:
            raise CommitCatalogError(
                reason="COMMIT_CATALOG_RUN_NOT_FOUND",
                detail="Commit catalog 실행 기록을 찾을 수 없습니다.",
                retryable=True,
                status_code=500,
            )
        run.state = state
        run.reason = reason
        run.detail = detail
        run.retryable = retryable
        run.unavailable_roots_json = unavailable_roots
        run.discovered_count = discovered_count
        run.persisted_count = persisted_count
        run.truncated = truncated
        run.shallow = shallow
        run.history_complete = history_complete
        run.finished_at = finished_at
        await self._session.flush()
        return run
