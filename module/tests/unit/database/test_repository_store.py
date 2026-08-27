"""Persistence behavior for Repository and active Branch bindings."""

from __future__ import annotations

import asyncio

import pytest

from backend.features.repositories.schemas import (
    BranchBindingCreateRequest,
    BranchBindingUpdateRequest,
    RepositoryCreateRequest,
    RepositoryUpdateRequest,
)
from backend.features.repositories.store import (
    BranchBindingStore,
    RepositoryStore,
    StoreLookupError,
)
from backend.infrastructure.database.base import Base
from backend.infrastructure.database.engine import create_engine_from_url, create_sessionmaker


def test_repository_and_binding_store_lifecycle() -> None:
    async def scenario() -> None:
        engine = create_engine_from_url(
            "sqlite+aiosqlite:///:memory:",
            execution_options={"schema_translate_map": {"snapshot": None}},
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)

            sessionmaker = create_sessionmaker(engine)
            async with sessionmaker() as session:
                repositories = RepositoryStore(session)
                bindings = BranchBindingStore(session)

                repository = await repositories.create(
                    RepositoryCreateRequest(
                        canonical_name="h5vision/vision",
                        display_name="Vision",
                        provider="github",
                        remote_url="https://github.com/h5vision/vision.git",
                        default_branch_ref="refs/heads/frontend",
                    )
                )
                binding = await bindings.create(
                    BranchBindingCreateRequest(
                        frontend_project_id="h5vision/vision",
                        repository_id=repository.repository_id,
                        branch_ref="refs/heads/frontend",
                        vss_project_id="vision--frontend",
                    )
                )
                await session.commit()

                resolved = await bindings.resolve_active(" h5vision/vision ")
                assert resolved.binding_id == binding.binding_id

                await repositories.update(
                    repository,
                    RepositoryUpdateRequest(display_name="Vision Frontend"),
                )
                await bindings.update(
                    binding,
                    BranchBindingUpdateRequest(vss_project_id="vision--frontend-v2"),
                )
                await session.commit()

                assert (await repositories.get(repository.repository_id)).display_name == (
                    "Vision Frontend"
                )
                assert (await bindings.get(binding.binding_id)).vss_project_id == (
                    "vision--frontend-v2"
                )

                await bindings.deactivate(binding)
                await session.commit()
                with pytest.raises(StoreLookupError) as error:
                    await bindings.resolve_active("h5vision/vision")
                assert error.value.reason == "SNAPSHOT_DESTINATION_REQUIRED"

                replacement = await bindings.create(
                    BranchBindingCreateRequest(
                        frontend_project_id="h5vision/vision",
                        repository_id=repository.repository_id,
                        branch_ref="refs/heads/module",
                        vss_project_id="vision--module",
                    )
                )
                await session.commit()
                assert replacement.active is True
                assert len(await bindings.list(frontend_project_id="h5vision/vision")) == 2

                await repositories.deactivate(repository)
                await session.commit()
                assert await repositories.list(active=True) == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_store_returns_stable_not_found_reason() -> None:
    async def scenario() -> None:
        from uuid import uuid4

        engine = create_engine_from_url(
            "sqlite+aiosqlite:///:memory:",
            execution_options={"schema_translate_map": {"snapshot": None}},
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            sessionmaker = create_sessionmaker(engine)
            async with sessionmaker() as session:
                with pytest.raises(StoreLookupError) as error:
                    await RepositoryStore(session).get(uuid4())
                assert error.value.reason == "REPOSITORY_NOT_FOUND"
        finally:
            await engine.dispose()

    asyncio.run(scenario())
