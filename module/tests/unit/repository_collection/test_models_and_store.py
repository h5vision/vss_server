"""수집 정본 모델 제약과 만료 가능한 저장소 sync lease 검증."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.features.repository_collection.errors import CollectionError
from backend.features.repository_collection.schemas import TrackedBranchCreateRequest
from backend.features.repository_collection.store import RepositoryCollectionStore
from backend.infrastructure.database.base import Base
from backend.infrastructure.database.engine import create_engine_from_url, create_sessionmaker
from backend.infrastructure.database.models import (
    BranchBinding,
    BranchHeadHistory,
    Repository,
    RepositorySyncRun,
    Snapshot,
    TrackedBranch,
)


def test_collector_snapshot_has_exactly_one_source_owner() -> None:
    engine = create_engine(
        "sqlite:///:memory:",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        repository = Repository(
            canonical_name="h5vision/collection",
            display_name="Collection",
            provider="github",
            remote_url="https://github.com/h5vision/collection.git",
            default_branch_ref="refs/heads/main",
        )
        session.add(repository)
        session.flush()
        tracked_branch = TrackedBranch(
            repository_id=repository.repository_id,
            branch_ref="refs/heads/main",
            vss_project_id="collection--main",
        )
        session.add(tracked_branch)
        session.flush()
        session.add(
            Snapshot(
                request_id=uuid4(),
                binding_id=None,
                tracked_branch_id=tracked_branch.tracked_branch_id,
                frontend_project_id=None,
                repository_id=repository.repository_id,
                branch_ref=tracked_branch.branch_ref,
                vss_project_id=tracked_branch.vss_project_id,
                base_revision="1" * 40,
                target_revision="2" * 40,
                source_type="remote_clone",
                state="validated",
            )
        )
        session.commit()
        assert session.scalar(select(Snapshot)).tracked_branch_id == (
            tracked_branch.tracked_branch_id
        )

        binding = BranchBinding(
            frontend_project_id="h5vision/collection",
            repository_id=repository.repository_id,
            branch_ref="refs/heads/main",
            vss_project_id="collection--legacy",
        )
        session.add(binding)
        session.flush()
        session.add(
            Snapshot(
                request_id=uuid4(),
                binding_id=binding.binding_id,
                tracked_branch_id=tracked_branch.tracked_branch_id,
                frontend_project_id="h5vision/collection",
                repository_id=repository.repository_id,
                branch_ref="refs/heads/main",
                vss_project_id="collection--invalid",
                base_revision="2" * 40,
                target_revision="3" * 40,
                source_type="remote_clone",
                state="validated",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
    engine.dispose()


def test_head_history_and_sync_run_constraints_are_persisted() -> None:
    engine = create_engine(
        "sqlite:///:memory:",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        repository = Repository(
            canonical_name="h5vision/history",
            display_name="History",
            provider="github",
            remote_url="https://github.com/h5vision/history.git",
            default_branch_ref="refs/heads/main",
        )
        session.add(repository)
        session.flush()
        branch = TrackedBranch(
            repository_id=repository.repository_id,
            branch_ref="refs/heads/main",
            vss_project_id="history--main",
            current_head_sha="1" * 40,
        )
        run = RepositorySyncRun(
            request_id=uuid4(),
            repository_id=repository.repository_id,
            trigger="manual",
            state="succeeded",
            reason="COLLECTION_SYNC_COMPLETED",
            detail="완료",
            retryable=False,
            lease_expires_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )
        session.add_all([branch, run])
        session.flush()
        session.add(
            BranchHeadHistory(
                tracked_branch_id=branch.tracked_branch_id,
                sync_run_id=run.sync_run_id,
                previous_head_sha=None,
                observed_head_sha="1" * 40,
                change_type="created",
            )
        )
        session.commit()
        assert session.scalar(select(BranchHeadHistory)).change_type == "created"

        session.add(
            RepositorySyncRun(
                request_id=uuid4(),
                repository_id=repository.repository_id,
                trigger="webhook",
                state="running",
                reason="invalid",
                detail="invalid",
                lease_expires_at=datetime.now(timezone.utc),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
    engine.dispose()


def test_sync_lease_blocks_active_run_and_recovers_expired_run() -> None:
    async def scenario() -> None:
        engine = create_engine_from_url("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            sessionmaker = create_sessionmaker(engine)
            async with sessionmaker() as session:
                repository = Repository(
                    canonical_name="h5vision/lease",
                    display_name="Lease",
                    provider="github",
                    remote_url="https://github.com/h5vision/lease.git",
                    default_branch_ref="refs/heads/main",
                )
                session.add(repository)
                await session.commit()
                repository_id = repository.repository_id

            async with sessionmaker() as session:
                _, first = await RepositoryCollectionStore(session).claim_sync(
                    repository_id,
                    request_id=uuid4(),
                    trigger="manual",
                    lease_seconds=300,
                )
                await session.commit()

            async with sessionmaker() as session:
                with pytest.raises(CollectionError) as error:
                    await RepositoryCollectionStore(session).claim_sync(
                        repository_id,
                        request_id=uuid4(),
                        trigger="periodic",
                        lease_seconds=300,
                    )
                assert error.value.reason == "COLLECTION_SYNC_ALREADY_RUNNING"
                await session.rollback()

            async with sessionmaker() as session:
                persisted = await session.get(RepositorySyncRun, first.sync_run_id)
                persisted.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                await session.commit()

            async with sessionmaker() as session:
                _, replacement = await RepositoryCollectionStore(session).claim_sync(
                    repository_id,
                    request_id=uuid4(),
                    trigger="periodic",
                    lease_seconds=300,
                )
                await session.commit()
                expired = await session.get(RepositorySyncRun, first.sync_run_id)
                assert expired.state == "failed"
                assert expired.reason == "COLLECTION_SYNC_LEASE_EXPIRED"
                assert replacement.state == "running"

            async with sessionmaker() as session:
                tracked = await RepositoryCollectionStore(session).create_tracked_branch(
                    TrackedBranchCreateRequest(
                        repository_id=repository_id,
                        branch_ref="refs/heads/main",
                        vss_project_id="lease--main",
                    )
                )
                await session.commit()
                assert tracked.current_head_sha is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())
