"""Repository Tag created, moved and deleted observation flow."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select

from backend.features.repository_collection.schemas import RemoteTag
from backend.features.repository_tags.service import RepositoryTagService
from backend.infrastructure.database.base import Base
from backend.infrastructure.database.engine import create_engine_from_url, create_sessionmaker
from backend.infrastructure.database.models import (
    Repository,
    RepositorySyncRun,
    RepositoryTag,
    TagRevisionHistory,
)


class FakeGitClient:
    def __init__(self) -> None:
        self.tags: list[RemoteTag] = []
        self.fetches: list[dict] = []

    def list_remote_tags(self, _remote_url: str, *, max_tags: int) -> list[RemoteTag]:
        assert max_tags == 5_000
        return self.tags

    def fetch_tag(self, **values) -> None:
        self.fetches.append(values)


def test_tag_service_preserves_created_moved_and_deleted_history() -> None:
    async def scenario() -> None:
        engine = create_engine_from_url("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            sessionmaker = create_sessionmaker(engine)
            async with sessionmaker() as session:
                repository = Repository(
                    canonical_name="h5vision/tags",
                    display_name="Tags",
                    provider="github",
                    remote_url="https://github.com/h5vision/tags.git",
                    default_branch_ref="refs/heads/main",
                )
                session.add(repository)
                await session.flush()
                run = RepositorySyncRun(
                    request_id=uuid4(),
                    repository_id=repository.repository_id,
                    trigger="manual",
                    state="running",
                    reason="COLLECTION_SYNC_RUNNING",
                    detail="running",
                    retryable=False,
                    lease_expires_at=datetime(2026, 9, 3, 1, tzinfo=timezone.utc),
                )
                session.add(run)
                await session.commit()
                repository_id = repository.repository_id
                sync_run_id = run.sync_run_id

            git_client = FakeGitClient()
            service = RepositoryTagService(
                sessionmaker=sessionmaker,
                git_client=git_client,
            )
            progress_calls = 0

            async def progress() -> None:
                nonlocal progress_calls
                progress_calls += 1

            git_client.tags = [
                RemoteTag(tag_ref="refs/tags/v1.0.0", commit_sha="1" * 40)
            ]
            created = await service.sync_repository(
                repository_id,
                sync_run_id=sync_run_id,
                progress=progress,
            )
            git_client.tags = [
                RemoteTag(tag_ref="refs/tags/v1.0.0", commit_sha="2" * 40)
            ]
            moved = await service.sync_repository(
                repository_id,
                sync_run_id=sync_run_id,
            )
            git_client.tags = []
            deleted = await service.sync_repository(
                repository_id,
                sync_run_id=sync_run_id,
            )

            assert created.created_count == 1
            assert moved.moved_count == 1
            assert deleted.deleted_count == 1
            assert len(git_client.fetches) == 2
            assert progress_calls == 3
            async with sessionmaker() as session:
                tag = await session.scalar(select(RepositoryTag))
                history = list(
                    await session.scalars(
                        select(TagRevisionHistory).order_by(
                            TagRevisionHistory.observed_at,
                            TagRevisionHistory.tag_history_id,
                        )
                    )
                )
                assert tag.current_commit_sha is None
                assert [item.change_type for item in history] == [
                    "created",
                    "moved",
                    "deleted",
                ]
                assert history[1].previous_commit_sha == "1" * 40
                assert history[1].observed_commit_sha == "2" * 40
        finally:
            await engine.dispose()

    asyncio.run(scenario())
