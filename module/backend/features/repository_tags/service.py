"""Remote Tag observation with Git commit verification and append-only history."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from backend.features.repository_collection.errors import CollectionError
from backend.features.repository_collection.git_client import RepositoryGitClient
from backend.features.repository_tags.schemas import RepositoryTagSyncResult
from backend.infrastructure.database.models import Repository, RepositoryTag, TagRevisionHistory


class RepositoryTagService:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        git_client: RepositoryGitClient,
        max_tags: int = 5_000,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._git_client = git_client
        self._max_tags = max_tags

    async def sync_repository(
        self,
        repository_id: UUID,
        *,
        sync_run_id: UUID,
        progress: Callable[[], Awaitable[None]] | None = None,
    ) -> RepositoryTagSyncResult:
        repository = await self._active_repository(repository_id)
        remote_tags = await run_in_threadpool(
            self._git_client.list_remote_tags,
            repository.remote_url,
            max_tags=self._max_tags,
        )
        if progress is not None:
            await progress()
        async with self._sessionmaker() as session:
            try:
                existing = {
                    item.tag_ref: item
                    for item in await session.scalars(
                        select(RepositoryTag).where(
                            RepositoryTag.repository_id == repository_id
                        )
                    )
                }
            except SQLAlchemyError as exc:
                raise self._database_failure() from exc

        remote_by_ref = {item.tag_ref: item for item in remote_tags}
        changed = [
            item
            for item in remote_tags
            if item.tag_ref not in existing
            or existing[item.tag_ref].current_commit_sha != item.commit_sha
        ]
        for item in changed:
            await run_in_threadpool(
                self._git_client.fetch_tag,
                repository_id=repository_id,
                remote_url=repository.remote_url,
                tag_ref=item.tag_ref,
                expected_commit_sha=item.commit_sha,
            )
            if progress is not None:
                await progress()

        observed_at = datetime.now(timezone.utc)
        counts = {"created": 0, "moved": 0, "deleted": 0, "recreated": 0}
        async with self._sessionmaker() as session:
            try:
                persisted = {
                    item.tag_ref: item
                    for item in await session.scalars(
                        select(RepositoryTag).where(
                            RepositoryTag.repository_id == repository_id
                        )
                    )
                }
                for tag_ref, remote in remote_by_ref.items():
                    tag = persisted.get(tag_ref)
                    if tag is None:
                        tag = RepositoryTag(
                            repository_id=repository_id,
                            tag_ref=tag_ref,
                            current_commit_sha=remote.commit_sha,
                            last_observed_at=observed_at,
                        )
                        session.add(tag)
                        await session.flush()
                        change_type = "created"
                        previous = None
                    elif tag.current_commit_sha == remote.commit_sha:
                        tag.last_observed_at = observed_at
                        continue
                    else:
                        previous = tag.current_commit_sha
                        change_type = "recreated" if previous is None else "moved"
                        tag.current_commit_sha = remote.commit_sha
                        tag.last_observed_at = observed_at
                    session.add(
                        TagRevisionHistory(
                            repository_tag_id=tag.repository_tag_id,
                            sync_run_id=sync_run_id,
                            previous_commit_sha=previous,
                            observed_commit_sha=remote.commit_sha,
                            change_type=change_type,
                            observed_at=observed_at,
                        )
                    )
                    counts[change_type] += 1

                for tag_ref, tag in persisted.items():
                    if tag_ref in remote_by_ref or tag.current_commit_sha is None:
                        continue
                    previous = tag.current_commit_sha
                    tag.current_commit_sha = None
                    tag.last_observed_at = observed_at
                    session.add(
                        TagRevisionHistory(
                            repository_tag_id=tag.repository_tag_id,
                            sync_run_id=sync_run_id,
                            previous_commit_sha=previous,
                            observed_commit_sha=None,
                            change_type="deleted",
                            observed_at=observed_at,
                        )
                    )
                    counts["deleted"] += 1
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise CollectionError(
                    reason="TAG_OBSERVATION_CONFLICT",
                    detail="같은 Repository Tag가 동시에 저장되었습니다.",
                    retryable=True,
                    status_code=409,
                ) from exc
            except SQLAlchemyError as exc:
                await session.rollback()
                raise self._database_failure() from exc
        if progress is not None:
            await progress()

        return RepositoryTagSyncResult(
            repository_id=repository_id,
            observed_count=len(remote_tags),
            created_count=counts["created"],
            moved_count=counts["moved"],
            deleted_count=counts["deleted"],
            recreated_count=counts["recreated"],
        )

    async def _active_repository(self, repository_id: UUID) -> Repository:
        async with self._sessionmaker() as session:
            try:
                repository = await session.get(Repository, repository_id)
            except SQLAlchemyError as exc:
                raise self._database_failure() from exc
        if repository is None:
            raise CollectionError(
                reason="REPOSITORY_NOT_FOUND",
                detail="Tag를 수집할 Repository를 찾을 수 없습니다.",
                retryable=False,
                status_code=404,
            )
        if not repository.active:
            raise CollectionError(
                reason="REPOSITORY_INACTIVE",
                detail="비활성 Repository의 Tag는 수집할 수 없습니다.",
                retryable=False,
                status_code=409,
            )
        return repository

    @staticmethod
    def _database_failure() -> CollectionError:
        return CollectionError(
            reason="DATABASE_UNAVAILABLE",
            detail="Tag 수집용 Snapshot 데이터베이스를 사용할 수 없습니다.",
            retryable=True,
            status_code=503,
        )
