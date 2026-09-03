"""Phase 7A PR/MR schema, constraints and append-only store tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from backend.features.change_requests.errors import ChangeRequestError
from backend.features.change_requests.schemas import ChangeRequestObservationRequest
from backend.features.change_requests.store import ChangeRequestStore
from backend.infrastructure.database.base import Base
from backend.infrastructure.database.engine import create_engine_from_url, create_sessionmaker
from backend.infrastructure.database.models import (
    ChangeRequest,
    ChangeRequestRevision,
    Repository,
)


def observation(repository_id, **changes) -> ChangeRequestObservationRequest:
    values = {
        "repository_id": repository_id,
        "provider": "github",
        "external_number": 42,
        "kind": "pull_request",
        "state": "open",
        "title": "Add revision context",
        "base_ref": "refs/heads/main",
        "head_ref": "refs/heads/feature/context",
        "base_sha": "1" * 40,
        "head_sha": "2" * 40,
        "observed_at": datetime(2026, 9, 2, tzinfo=timezone.utc),
    }
    values.update(changes)
    return ChangeRequestObservationRequest.model_validate(values)


def test_change_request_contract_requires_provider_kind_and_real_merge_revision() -> None:
    with pytest.raises(ValidationError):
        ChangeRequestObservationRequest.model_validate(
            {
                **observation("00000000-0000-0000-0000-000000000001").model_dump(),
                "kind": "merge_request",
            }
        )

    with pytest.raises(ValidationError):
        ChangeRequestObservationRequest.model_validate(
            {
                **observation("00000000-0000-0000-0000-000000000001").model_dump(),
                "state": "merged",
            }
        )


def test_observation_is_idempotent_and_head_changes_are_append_only() -> None:
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

            first_request = observation(repository_id)
            async with sessionmaker() as session:
                store = ChangeRequestStore(session)
                current, first, created = await store.observe(first_request)
                await session.commit()
                assert created is True
                change_request_id = current.change_request_id
                first_observation_id = first.revision_observation_id

            async with sessionmaker() as session:
                store = ChangeRequestStore(session)
                _, duplicate, created = await store.observe(first_request)
                await session.commit()
                assert created is False
                assert duplicate.revision_observation_id == first_observation_id

            async with sessionmaker() as session:
                store = ChangeRequestStore(session)
                current, changed, created = await store.observe(
                    observation(
                        repository_id,
                        head_sha="3" * 40,
                        observed_at=datetime(2026, 9, 2, 1, tzinfo=timezone.utc),
                    )
                )
                await session.commit()
                assert created is True
                assert current.current_head_sha == "3" * 40
                assert changed.revision_observation_id != first_observation_id

            async with sessionmaker() as session:
                store = ChangeRequestStore(session)
                current, _, created = await store.observe(first_request)
                await session.commit()
                assert created is False
                assert current.current_head_sha == "3" * 40

            async with sessionmaker() as session:
                assert await session.scalar(
                    select(func.count()).select_from(ChangeRequest)
                ) == 1
                assert await session.scalar(
                    select(func.count()).select_from(ChangeRequestRevision)
                ) == 2
                persisted = await session.get(ChangeRequest, change_request_id)
                assert persisted.current_head_sha == "3" * 40
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_observation_rejects_repository_provider_mismatch() -> None:
    async def scenario() -> None:
        engine = create_engine_from_url("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            sessionmaker = create_sessionmaker(engine)
            async with sessionmaker() as session:
                repository = Repository(
                    canonical_name="h5vision/gitlab-context",
                    display_name="GitLab Context",
                    provider="gitlab",
                    remote_url="https://gitlab.example/h5vision/context.git",
                    default_branch_ref="refs/heads/main",
                )
                session.add(repository)
                await session.commit()
                repository_id = repository.repository_id

            async with sessionmaker() as session:
                with pytest.raises(ChangeRequestError) as error:
                    await ChangeRequestStore(session).observe(observation(repository_id))
                assert error.value.reason == "CHANGE_REQUEST_PROVIDER_MISMATCH"
        finally:
            await engine.dispose()

    asyncio.run(scenario())
