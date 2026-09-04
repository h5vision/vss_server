"""Bounded Repository commit graph catalog orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from backend.features.commit_catalog.errors import CommitCatalogError
from backend.features.commit_catalog.schemas import CommitCatalogResult
from backend.features.commit_catalog.store import CommitCatalogStore
from backend.features.repository_collection.git_client import RepositoryGitClient
from backend.infrastructure.database.models import (
    BranchHeadHistory,
    ChangeRequest,
    ChangeRequestRevision,
    RepositoryTag,
    Snapshot,
    TagRevisionHistory,
    TrackedBranch,
)


class CommitCatalogService:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        git_client: RepositoryGitClient,
        max_commits: int,
        batch_size: int,
        timeout_seconds: float,
        lease_seconds: int,
        subject_max_length: int,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._git_client = git_client
        self._max_commits = max_commits
        self._batch_size = batch_size
        self._timeout_seconds = timeout_seconds
        self._lease_seconds = lease_seconds
        self._subject_max_length = subject_max_length

    async def catalog_repository(
        self,
        repository_id: UUID,
        *,
        request_id: UUID | None = None,
    ) -> CommitCatalogResult:
        roots = await self._catalog_roots(repository_id)
        resolved_request_id = request_id or uuid4()
        run = await self._claim_run(
            repository_id,
            request_id=resolved_request_id,
            roots=roots,
        )
        started_at = self._as_utc(run.started_at)
        try:
            scan = await run_in_threadpool(
                self._git_client.scan_commit_graph,
                repository_id=repository_id,
                roots=roots,
                max_commits=self._max_commits,
                timeout_seconds=self._timeout_seconds,
                subject_max_length=self._subject_max_length,
            )
            observed_at = datetime.now(timezone.utc)
            async with self._sessionmaker() as session:
                persisted_count = await CommitCatalogStore(session).persist_scan(
                    repository_id,
                    scan,
                    batch_size=self._batch_size,
                    observed_at=observed_at,
                )
                await session.commit()
            reason = (
                "COMMIT_CATALOG_PARTIAL"
                if not scan.history_complete
                else "COMMIT_CATALOG_COMPLETED"
            )
            detail = (
                "Repository commit graph를 제한 범위까지 저장했습니다."
                if not scan.history_complete
                else "Repository commit graph를 검증하고 저장했습니다."
            )
            finished_at = datetime.now(timezone.utc)
            await self._finish_run(
                run.run_id,
                state="succeeded",
                reason=reason,
                detail=detail,
                retryable=False,
                unavailable_roots=scan.unavailable_roots,
                discovered_count=len(scan.entries),
                persisted_count=persisted_count,
                truncated=scan.truncated,
                shallow=scan.shallow,
                history_complete=scan.history_complete,
                finished_at=finished_at,
            )
            return CommitCatalogResult(
                ok=True,
                reason=reason,
                detail=detail,
                retryable=False,
                run_id=run.run_id,
                repository_id=repository_id,
                roots=scan.roots,
                unavailable_roots=scan.unavailable_roots,
                discovered_count=len(scan.entries),
                persisted_count=persisted_count,
                truncated=scan.truncated,
                shallow=scan.shallow,
                history_complete=scan.history_complete,
                started_at=started_at,
                finished_at=finished_at,
            )
        except CommitCatalogError as exc:
            finished_at = datetime.now(timezone.utc)
            await self._finish_run(
                run.run_id,
                state="failed",
                reason=exc.reason,
                detail=exc.detail,
                retryable=exc.retryable,
                unavailable_roots=[],
                discovered_count=0,
                persisted_count=0,
                truncated=False,
                shallow=False,
                history_complete=False,
                finished_at=finished_at,
            )
            raise
        except SQLAlchemyError as exc:
            failure = self._database_failure()
            finished_at = datetime.now(timezone.utc)
            await self._finish_run(
                run.run_id,
                state="failed",
                reason=failure.reason,
                detail=failure.detail,
                retryable=failure.retryable,
                unavailable_roots=[],
                discovered_count=0,
                persisted_count=0,
                truncated=False,
                shallow=False,
                history_complete=False,
                finished_at=finished_at,
            )
            raise failure from exc

    async def _catalog_roots(self, repository_id: UUID) -> list[str]:
        async with self._sessionmaker() as session:
            try:
                roots: set[str] = set()
                roots.update(
                    value
                    for value in await session.scalars(
                        select(TrackedBranch.current_head_sha).where(
                            TrackedBranch.repository_id == repository_id,
                            TrackedBranch.tracked.is_(True),
                            TrackedBranch.current_head_sha.is_not(None),
                        )
                    )
                    if value is not None
                )
                for previous, observed in await session.execute(
                    select(
                        BranchHeadHistory.previous_head_sha,
                        BranchHeadHistory.observed_head_sha,
                    ).join(TrackedBranch)
                    .where(TrackedBranch.repository_id == repository_id)
                ):
                    roots.update(value for value in (previous, observed) if value is not None)
                for base, target in await session.execute(
                    select(Snapshot.base_revision, Snapshot.target_revision).where(
                        Snapshot.repository_id == repository_id
                    )
                ):
                    roots.update((base, target))
                for base, head, merge in await session.execute(
                    select(
                        ChangeRequest.current_base_sha,
                        ChangeRequest.current_head_sha,
                        ChangeRequest.current_merge_sha,
                    ).where(ChangeRequest.repository_id == repository_id)
                ):
                    roots.update(value for value in (base, head, merge) if value is not None)
                roots.update(
                    value
                    for value in await session.scalars(
                        select(RepositoryTag.current_commit_sha).where(
                            RepositoryTag.repository_id == repository_id,
                            RepositoryTag.current_commit_sha.is_not(None),
                        )
                    )
                    if value is not None
                )
                for previous, observed in await session.execute(
                    select(
                        TagRevisionHistory.previous_commit_sha,
                        TagRevisionHistory.observed_commit_sha,
                    )
                    .join(RepositoryTag)
                    .where(RepositoryTag.repository_id == repository_id)
                ):
                    roots.update(value for value in (previous, observed) if value is not None)
                for base, head, merge in await session.execute(
                    select(
                        ChangeRequestRevision.base_sha,
                        ChangeRequestRevision.head_sha,
                        ChangeRequestRevision.merge_sha,
                    )
                    .join(ChangeRequest)
                    .where(ChangeRequest.repository_id == repository_id)
                ):
                    roots.update(value for value in (base, head, merge) if value is not None)
            except SQLAlchemyError as exc:
                raise self._database_failure() from exc
        normalized = sorted(value.lower() for value in roots)
        if not normalized:
            raise CommitCatalogError(
                reason="COMMIT_CATALOG_ROOTS_REQUIRED",
                detail="추적 Branch, Snapshot 또는 PR/MR에서 catalog root를 찾지 못했습니다.",
                retryable=False,
                status_code=409,
            )
        return normalized

    async def _claim_run(
        self,
        repository_id: UUID,
        *,
        request_id: UUID,
        roots: list[str],
    ):
        async with self._sessionmaker() as session:
            try:
                _, run = await CommitCatalogStore(session).claim_run(
                    repository_id,
                    request_id=request_id,
                    roots=roots,
                    max_commits=self._max_commits,
                    lease_seconds=self._lease_seconds,
                )
                await session.commit()
                return run
            except CommitCatalogError:
                await session.rollback()
                raise
            except IntegrityError as exc:
                await session.rollback()
                raise CommitCatalogError(
                    reason="COMMIT_CATALOG_ALREADY_RUNNING",
                    detail="같은 Repository의 Commit catalog 작업이 이미 진행 중입니다.",
                    retryable=True,
                    status_code=409,
                ) from exc
            except SQLAlchemyError as exc:
                await session.rollback()
                raise self._database_failure() from exc

    async def _finish_run(self, run_id: UUID, **values) -> None:
        async with self._sessionmaker() as session:
            try:
                await CommitCatalogStore(session).finish_run(run_id, **values)
                await session.commit()
            except CommitCatalogError:
                await session.rollback()
                raise
            except SQLAlchemyError as exc:
                await session.rollback()
                raise self._database_failure() from exc

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _database_failure() -> CommitCatalogError:
        return CommitCatalogError(
            reason="DATABASE_UNAVAILABLE",
            detail="Commit catalog용 Snapshot 데이터베이스를 사용할 수 없습니다.",
            retryable=True,
            status_code=503,
        )
