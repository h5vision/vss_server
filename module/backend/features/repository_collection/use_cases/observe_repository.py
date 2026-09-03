"""Use case for observing remote repositories and cataloging branch references."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from backend.features.repository_collection.errors import CollectionError
from backend.features.repository_collection.schemas import RepositoryCatalogResult
from backend.infrastructure.database.models import Repository
from backend.ports.git import RemoteRefReader


@dataclass(frozen=True, slots=True)
class ObserveRepositoryUseCase:
    """Observes remote repository branches and verifies default branch existence."""

    sessionmaker: async_sessionmaker[AsyncSession]
    ref_reader: RemoteRefReader

    async def catalog_repository(self, repository_id: UUID) -> RepositoryCatalogResult:
        repository = await self.get_active_repository(repository_id)
        branches = await run_in_threadpool(
            self.ref_reader.list_remote_heads,
            repository.remote_url,
        )
        return RepositoryCatalogResult(
            repository_id=repository.repository_id,
            default_branch_ref=repository.default_branch_ref,
            default_branch_exists=any(
                item.branch_ref == repository.default_branch_ref for item in branches
            ),
            branches=branches,
        )

    async def validate_repository(self, repository_id: UUID) -> RepositoryCatalogResult:
        catalog = await self.catalog_repository(repository_id)
        if not catalog.default_branch_exists:
            raise CollectionError(
                reason="REPOSITORY_DEFAULT_BRANCH_NOT_FOUND",
                detail="Repository의 기본 Branch를 원격에서 찾을 수 없습니다.",
                retryable=False,
                status_code=409,
            )
        return catalog

    async def get_active_repository(self, repository_id: UUID) -> Repository:
        async with self.sessionmaker() as session:
            try:
                repository = await session.get(Repository, repository_id)
            except SQLAlchemyError as exc:
                raise self._database_failure() from exc
            if repository is None:
                raise CollectionError(
                    reason="REPOSITORY_NOT_FOUND",
                    detail="요청한 Repository를 찾을 수 없습니다.",
                    retryable=False,
                    status_code=404,
                )
            if not repository.active:
                raise CollectionError(
                    reason="REPOSITORY_INACTIVE",
                    detail="비활성화된 Repository는 수집할 수 없습니다.",
                    retryable=False,
                    status_code=409,
                )
            return repository

    @staticmethod
    def _database_failure() -> CollectionError:
        return CollectionError(
            reason="DATABASE_UNAVAILABLE",
            detail="Repository 수집용 Snapshot 데이터베이스를 사용할 수 없습니다.",
            retryable=True,
            status_code=503,
        )
