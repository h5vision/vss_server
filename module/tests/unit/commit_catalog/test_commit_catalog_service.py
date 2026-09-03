"""Phase 7A-2 commit catalog roots, persistence and idempotency tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import func, select

from backend.features.commit_catalog.schemas import CommitGraphEntry, CommitGraphScanResult
from backend.features.commit_catalog.service import CommitCatalogService
from backend.infrastructure.database.base import Base
from backend.infrastructure.database.engine import create_engine_from_url, create_sessionmaker
from backend.infrastructure.database.models import (
    ChangeRequest,
    CommitCatalogRun,
    Repository,
    RepositoryCommit,
    RepositoryCommitParent,
    RepositoryTag,
    Snapshot,
    TrackedBranch,
)

NOW = datetime(2026, 9, 2, tzinfo=timezone.utc)
SHA1 = "1" * 40
SHA2 = "2" * 40
SHA3 = "3" * 40
SHA4 = "4" * 40


def entry(commit_sha: str, parent_shas: list[str], subject: str) -> CommitGraphEntry:
    return CommitGraphEntry(
        commit_sha=commit_sha,
        tree_sha=commit_sha,
        parent_shas=parent_shas,
        author_name="Catalog Test",
        authored_at=NOW,
        committed_at=NOW,
        subject=subject,
    )


class FakeGitClient:
    def __init__(self, scan: CommitGraphScanResult) -> None:
        self.scan = scan
        self.received_roots: list[str] = []

    def scan_commit_graph(self, **values) -> CommitGraphScanResult:
        self.received_roots = values["roots"]
        return self.scan.model_copy(update={"roots": values["roots"]})


def test_catalog_backfills_all_existing_roots_and_is_idempotent() -> None:
    async def scenario() -> None:
        engine = create_engine_from_url("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            sessionmaker = create_sessionmaker(engine)
            async with sessionmaker() as session:
                repository = Repository(
                    canonical_name="h5vision/catalog",
                    display_name="Catalog",
                    provider="github",
                    remote_url="https://github.com/h5vision/catalog.git",
                    default_branch_ref="refs/heads/main",
                )
                session.add(repository)
                await session.flush()
                branch = TrackedBranch(
                    repository_id=repository.repository_id,
                    branch_ref="refs/heads/main",
                    vss_project_id="catalog--main",
                    current_head_sha=SHA3,
                )
                session.add(branch)
                await session.flush()
                session.add(
                    Snapshot(
                        request_id=uuid4(),
                        binding_id=None,
                        tracked_branch_id=branch.tracked_branch_id,
                        frontend_project_id=None,
                        repository_id=repository.repository_id,
                        branch_ref=branch.branch_ref,
                        vss_project_id=branch.vss_project_id,
                        base_revision=SHA1,
                        target_revision=SHA3,
                        source_type="remote_clone",
                        state="completed",
                        vss_state="done",
                    )
                )
                session.add(
                    ChangeRequest(
                        repository_id=repository.repository_id,
                        provider="github",
                        external_number=7,
                        kind="pull_request",
                        state="merged",
                        title="Catalog history",
                        base_ref="refs/heads/main",
                        head_ref="refs/heads/feature/catalog",
                        current_base_sha=SHA1,
                        current_head_sha=SHA2,
                        current_merge_sha=SHA3,
                        last_observed_at=NOW,
                        provider_updated_at=NOW,
                        merged_at=NOW,
                    )
                )
                session.add(
                    RepositoryTag(
                        repository_id=repository.repository_id,
                        tag_ref="refs/tags/v1.0.0",
                        current_commit_sha=SHA4,
                        last_observed_at=NOW,
                    )
                )
                await session.commit()
                repository_id = repository.repository_id

            scan = CommitGraphScanResult(
                roots=[],
                unavailable_roots=[],
                entries=[
                    entry(SHA3, [SHA2], "third"),
                    entry(SHA2, [SHA1], "second"),
                    entry(SHA1, [], "first"),
                    entry(SHA4, [], "release"),
                ],
                truncated=False,
                shallow=False,
                history_complete=True,
            )
            git_client = FakeGitClient(scan)
            service = CommitCatalogService(
                sessionmaker=sessionmaker,
                git_client=git_client,
                max_commits=100,
                batch_size=2,
                timeout_seconds=30,
                lease_seconds=300,
                subject_max_length=256,
            )
            first = await service.catalog_repository(repository_id)
            second = await service.catalog_repository(repository_id)

            assert first.ok is True
            assert second.ok is True
            assert git_client.received_roots == [SHA1, SHA2, SHA3, SHA4]
            async with sessionmaker() as session:
                assert await session.scalar(
                    select(func.count()).select_from(RepositoryCommit)
                ) == 4
                assert await session.scalar(
                    select(func.count()).select_from(RepositoryCommitParent)
                ) == 2
                assert await session.scalar(
                    select(func.count()).select_from(CommitCatalogRun)
                ) == 2
                edges = list(
                    await session.scalars(
                        select(RepositoryCommitParent).order_by(
                            RepositoryCommitParent.parent_sha
                        )
                    )
                )
                assert all(edge.parent_commit_id is not None for edge in edges)
                assert all(edge.parent_missing_reason is None for edge in edges)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_truncated_catalog_preserves_unresolved_parent_sha() -> None:
    async def scenario() -> None:
        engine = create_engine_from_url("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            sessionmaker = create_sessionmaker(engine)
            async with sessionmaker() as session:
                repository = Repository(
                    canonical_name="h5vision/truncated",
                    display_name="Truncated",
                    provider="github",
                    remote_url="https://github.com/h5vision/truncated.git",
                    default_branch_ref="refs/heads/main",
                )
                session.add(repository)
                await session.flush()
                session.add(
                    TrackedBranch(
                        repository_id=repository.repository_id,
                        branch_ref="refs/heads/main",
                        vss_project_id="truncated--main",
                        current_head_sha=SHA3,
                    )
                )
                await session.commit()
                repository_id = repository.repository_id

            git_client = FakeGitClient(
                CommitGraphScanResult(
                    roots=[],
                    unavailable_roots=[],
                    entries=[entry(SHA3, [SHA2], "third")],
                    truncated=True,
                    shallow=False,
                    history_complete=False,
                )
            )
            result = await CommitCatalogService(
                sessionmaker=sessionmaker,
                git_client=git_client,
                max_commits=1,
                batch_size=1,
                timeout_seconds=30,
                lease_seconds=300,
                subject_max_length=256,
            ).catalog_repository(repository_id)

            assert result.reason == "COMMIT_CATALOG_PARTIAL"
            async with sessionmaker() as session:
                edge = await session.scalar(select(RepositoryCommitParent))
                assert edge.parent_sha == SHA2
                assert edge.parent_commit_id is None
                assert edge.parent_missing_reason == "scan_truncated"
        finally:
            await engine.dispose()

    asyncio.run(scenario())
