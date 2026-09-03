"""Read-only provider metadata collection with Git object verification."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol
from uuid import UUID

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from backend.features.change_requests.errors import ChangeRequestError
from backend.features.change_requests.schemas import (
    ChangeRequestCollectionResult,
    ChangeRequestObservationRequest,
)
from backend.features.change_requests.store import ChangeRequestStore
from backend.features.repository_collection.errors import CollectionError
from backend.features.repository_collection.git_client import RepositoryGitClient
from backend.infrastructure.database.models import Repository
from backend.integrations.change_requests.errors import ChangeRequestProviderError


class ChangeRequestProviderClient(Protocol):
    def list_change_requests(
        self,
        *,
        repository_id: UUID,
        canonical_name: str,
        remote_url: str | None = None,
    ) -> list[ChangeRequestObservationRequest]: ...


class ChangeRequestCollectionService:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        git_client: RepositoryGitClient,
        provider_clients: dict[str, ChangeRequestProviderClient],
    ) -> None:
        self._sessionmaker = sessionmaker
        self._git_client = git_client
        self._provider_clients = provider_clients

    def supports(self, provider: str) -> bool:
        return provider.lower() in self._provider_clients

    async def sync_repository(
        self,
        repository_id: UUID,
        *,
        progress: Callable[[], Awaitable[None]] | None = None,
    ) -> ChangeRequestCollectionResult:
        repository = await self._active_repository(repository_id)
        provider = repository.provider.lower()
        client = self._provider_clients.get(provider)
        if client is None:
            raise ChangeRequestError(
                reason="CHANGE_REQUEST_PROVIDER_UNSUPPORTED",
                detail="Repository provider에 사용할 PR/MR read-only client가 없습니다.",
                retryable=False,
                status_code=409,
            )
        try:
            observations = await run_in_threadpool(
                client.list_change_requests,
                repository_id=repository_id,
                canonical_name=repository.canonical_name,
                remote_url=repository.remote_url,
            )
        except ChangeRequestProviderError as exc:
            raise ChangeRequestError(
                reason=exc.reason,
                detail="Git provider에서 PR/MR metadata를 안전하게 조회하지 못했습니다.",
                retryable=exc.retryable,
                status_code=503 if exc.retryable else 409,
            ) from exc
        if progress is not None:
            await progress()

        created_revision_count = 0
        for observation in observations:
            if observation.repository_id != repository_id or observation.provider != provider:
                raise ChangeRequestError(
                    reason="CHANGE_REQUEST_PROVIDER_CONTRACT_MISMATCH",
                    detail="Provider client가 다른 Repository 또는 provider 결과를 반환했습니다.",
                    retryable=False,
                    status_code=502,
                )
            async with self._sessionmaker() as session:
                try:
                    already_observed = await ChangeRequestStore(session).has_observation(
                        observation
                    )
                except SQLAlchemyError as exc:
                    raise self._database_failure() from exc
            if not already_observed:
                try:
                    await run_in_threadpool(
                        self._git_client.fetch_change_request_revisions,
                        repository_id=repository_id,
                        remote_url=repository.remote_url,
                        provider=provider,
                        external_number=observation.external_number,
                        base_ref=observation.base_ref,
                        base_sha=observation.base_sha,
                        head_sha=observation.head_sha,
                        merge_sha=observation.merge_sha,
                    )
                except CollectionError as exc:
                    raise ChangeRequestError(
                        reason=exc.reason,
                        detail=exc.detail,
                        retryable=exc.retryable,
                        status_code=exc.status_code,
                    ) from exc
            async with self._sessionmaker() as session:
                try:
                    _, _, created = await ChangeRequestStore(session).observe(observation)
                    await session.commit()
                except ChangeRequestError:
                    await session.rollback()
                    raise
                except IntegrityError as exc:
                    await session.rollback()
                    raise ChangeRequestError(
                        reason="CHANGE_REQUEST_OBSERVATION_CONFLICT",
                        detail="같은 PR/MR revision이 동시에 저장되었습니다.",
                        retryable=True,
                        status_code=409,
                    ) from exc
                except SQLAlchemyError as exc:
                    await session.rollback()
                    raise self._database_failure() from exc
            created_revision_count += int(created)
            if progress is not None:
                await progress()

        return ChangeRequestCollectionResult(
            ok=True,
            reason="CHANGE_REQUEST_COLLECTION_COMPLETED",
            detail="Git provider의 PR/MR revision을 Git object와 대조해 저장했습니다.",
            retryable=False,
            repository_id=repository_id,
            provider=provider,
            observed_count=len(observations),
            created_revision_count=created_revision_count,
        )

    async def _active_repository(self, repository_id: UUID) -> Repository:
        async with self._sessionmaker() as session:
            try:
                repository = await session.get(Repository, repository_id)
            except SQLAlchemyError as exc:
                raise self._database_failure() from exc
        if repository is None:
            raise ChangeRequestError(
                reason="REPOSITORY_NOT_FOUND",
                detail="PR/MR를 수집할 Repository를 찾을 수 없습니다.",
                retryable=False,
                status_code=404,
            )
        if not repository.active:
            raise ChangeRequestError(
                reason="REPOSITORY_INACTIVE",
                detail="비활성 Repository의 PR/MR는 수집할 수 없습니다.",
                retryable=False,
                status_code=409,
            )
        return repository

    @staticmethod
    def _database_failure() -> ChangeRequestError:
        return ChangeRequestError(
            reason="DATABASE_UNAVAILABLE",
            detail="PR/MR 수집용 Snapshot 데이터베이스를 사용할 수 없습니다.",
            retryable=True,
            status_code=503,
        )
