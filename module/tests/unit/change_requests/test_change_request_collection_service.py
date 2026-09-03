"""Provider metadata to verified Git ref and append-only PR/MR catalog flow."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import func, select

from backend.features.change_requests.schemas import ChangeRequestObservationRequest
from backend.features.change_requests.service import ChangeRequestCollectionService
from backend.infrastructure.database.base import Base
from backend.infrastructure.database.engine import create_engine_from_url, create_sessionmaker
from backend.infrastructure.database.models import (
    ChangeRequest,
    ChangeRequestRevision,
    Repository,
)


class FakeProviderClient:
    def __init__(self, observations: list[ChangeRequestObservationRequest]) -> None:
        self.observations = observations
        self.calls = 0

    def list_change_requests(self, **_values) -> list[ChangeRequestObservationRequest]:
        self.calls += 1
        return self.observations


class FakeGitClient:
    def __init__(self) -> None:
        self.fetches: list[dict] = []

    def fetch_change_request_revisions(self, **values) -> None:
        self.fetches.append(values)


def test_provider_sync_verifies_new_revision_once_and_updates_catalog_idempotently() -> None:
    async def scenario() -> None:
        engine = create_engine_from_url("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            sessionmaker = create_sessionmaker(engine)
            async with sessionmaker() as session:
                repository = Repository(
                    canonical_name="h5vision/context",
                    display_name="Context",
                    provider="github",
                    remote_url="https://github.com/h5vision/context.git",
                    default_branch_ref="refs/heads/main",
                )
                session.add(repository)
                await session.commit()
                repository_id = repository.repository_id

            observation = ChangeRequestObservationRequest(
                repository_id=repository_id,
                provider="github",
                external_number=42,
                kind="pull_request",
                state="open",
                title="Context API",
                base_ref="refs/heads/main",
                head_ref="refs/heads/feature/context",
                base_sha="1" * 40,
                head_sha="2" * 40,
                observed_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
                provider_updated_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
            )
            provider = FakeProviderClient([observation])
            git_client = FakeGitClient()
            service = ChangeRequestCollectionService(
                sessionmaker=sessionmaker,
                git_client=git_client,
                provider_clients={"github": provider},
            )

            progress_calls = 0

            async def progress() -> None:
                nonlocal progress_calls
                progress_calls += 1

            first = await service.sync_repository(repository_id, progress=progress)
            second = await service.sync_repository(repository_id)

            assert first.ok is True
            assert first.observed_count == 1
            assert first.created_revision_count == 1
            assert second.created_revision_count == 0
            assert provider.calls == 2
            assert len(git_client.fetches) == 1
            assert progress_calls == 2
            assert git_client.fetches[0]["head_sha"] == "2" * 40
            async with sessionmaker() as session:
                assert await session.scalar(
                    select(func.count()).select_from(ChangeRequest)
                ) == 1
                assert await session.scalar(
                    select(func.count()).select_from(ChangeRequestRevision)
                ) == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())
