"""실제 PostgreSQL에서 migration, 제약과 row lock을 검증한다.

기본 pytest에는 포함하지 않고 ``scripts/verify_postgresql_17.py``가 임시 DB를 준비한 뒤
명시적으로 실행한다.
"""

from __future__ import annotations

import asyncio
import os
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from backend.features.snapshots.store import SnapshotStore
from backend.infrastructure.database.engine import create_engine_from_url, create_sessionmaker
from backend.infrastructure.database.models import BranchBinding, Repository, Snapshot


def database_url() -> str:
    value = os.environ.get("SNAPSHOT_TEST_POSTGRES_URL", "")
    if not value.startswith("postgresql+asyncpg://"):
        raise RuntimeError("SNAPSHOT_TEST_POSTGRES_URL에 asyncpg PostgreSQL URL이 필요합니다.")
    return value


def test_migration_created_snapshot_schema_and_version_table() -> None:
    async def scenario() -> None:
        engine = create_engine_from_url(database_url())
        async with engine.connect() as connection:
            version = await connection.scalar(
                text("SELECT version_num FROM snapshot.alembic_version")
            )
            table_count = await connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'snapshot'"
                )
            )
        await engine.dispose()
        assert version == "0003_workspace_id"
        assert table_count == 7

    asyncio.run(scenario())


def test_unique_snapshot_target_is_enforced_under_concurrent_transactions() -> None:
    async def scenario() -> None:
        engine = create_engine_from_url(database_url())
        sessionmaker = create_sessionmaker(engine)
        repository_id, binding_id = await seed_binding(sessionmaker)
        target_revision = uuid4().hex + "12345678"

        async def insert_once() -> bool:
            async with sessionmaker() as session:
                session.add(
                    Snapshot(
                        request_id=uuid4(),
                        binding_id=binding_id,
                        frontend_project_id="postgres/concurrent",
                        repository_id=repository_id,
                        branch_ref="refs/heads/main",
                        vss_project_id="postgres--concurrent",
                        base_revision="1" * 40,
                        target_revision=target_revision,
                        source_type="remote_clone",
                        state="failed",
                    )
                )
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    return False
                return True

        results = await asyncio.gather(insert_once(), insert_once())
        assert sorted(results) == [False, True]

        async with sessionmaker() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(Snapshot)
                .where(
                    Snapshot.vss_project_id == "postgres--concurrent",
                    Snapshot.target_revision == target_revision,
                )
            )
        await engine.dispose()
        assert count == 1

    asyncio.run(scenario())


def test_snapshot_row_lock_serializes_manual_retry_claims() -> None:
    async def scenario() -> None:
        engine = create_engine_from_url(database_url())
        sessionmaker = create_sessionmaker(engine)
        repository_id, binding_id = await seed_binding(sessionmaker)
        snapshot_id = await seed_snapshot(sessionmaker, repository_id, binding_id)

        async with sessionmaker() as first_session:
            first = await SnapshotStore(first_session).get_for_update(snapshot_id)
            assert first is not None

            async def wait_for_same_snapshot() -> str:
                async with sessionmaker() as second_session:
                    second = await SnapshotStore(second_session).get_for_update(snapshot_id)
                    assert second is not None
                    observed_state = second.state
                    await second_session.rollback()
                    return observed_state

            waiter = asyncio.create_task(wait_for_same_snapshot())
            await asyncio.sleep(0.2)
            assert not waiter.done()

            first.state = "accepted"
            await first_session.commit()
            observed_state = await asyncio.wait_for(waiter, timeout=5)

        await engine.dispose()
        assert observed_state == "accepted"

    asyncio.run(scenario())


async def seed_binding(sessionmaker) -> tuple[UUID, UUID]:
    suffix = uuid4().hex
    async with sessionmaker() as session:
        repository = Repository(
            canonical_name=f"postgres/{suffix}",
            display_name="PostgreSQL 검증",
            provider="test",
            remote_url=f"https://example.invalid/{suffix}.git",
            default_branch_ref="refs/heads/main",
        )
        session.add(repository)
        await session.flush()
        binding = BranchBinding(
            frontend_project_id=f"postgres/{suffix}",
            frontend_workspace_name=f"workspace-{suffix}",
            repository_id=repository.repository_id,
            branch_ref="refs/heads/main",
            vss_project_id=f"postgres--{suffix}",
            active=True,
        )
        session.add(binding)
        await session.commit()
        return repository.repository_id, binding.binding_id


async def seed_snapshot(sessionmaker, repository_id: UUID, binding_id: UUID) -> UUID:
    async with sessionmaker() as session:
        snapshot = Snapshot(
            request_id=uuid4(),
            binding_id=binding_id,
            frontend_project_id=f"postgres/{uuid4().hex}",
            repository_id=repository_id,
            branch_ref="refs/heads/main",
            vss_project_id=f"postgres--lock-{uuid4().hex}",
            base_revision="1" * 40,
            target_revision="2" * 40,
            source_type="remote_clone",
            state="failed",
        )
        session.add(snapshot)
        await session.commit()
        return snapshot.snapshot_id
