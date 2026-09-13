"""Actual local Git cache to commit catalog integration."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from sqlalchemy import func, select

from backend.features.commit_catalog.service import CommitCatalogService
from backend.features.repository_collection.git_client import RepositoryGitClient
from backend.infrastructure.database.base import Base
from backend.infrastructure.database.engine import create_engine_from_url, create_sessionmaker
from backend.infrastructure.database.models import (
    CommitCatalogRun,
    Repository,
    RepositoryCommit,
    RepositoryCommitParent,
    TrackedBranch,
)


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def test_actual_git_graph_is_persisted_with_ordered_parents(tmp_path: Path) -> None:
    async def scenario() -> None:
        remote = tmp_path / "remote.git"
        work = tmp_path / "work"
        remote.mkdir()
        work.mkdir()
        git(remote, "init", "--bare")
        git(work, "init", "-b", "main")
        git(work, "config", "user.email", "catalog@example.invalid")
        git(work, "config", "user.name", "Catalog Integration")
        git(work, "remote", "add", "origin", str(remote))
        (work / "app.py").write_text("VERSION = 1\n", "utf-8")
        git(work, "add", "--all")
        git(work, "commit", "-m", "first")
        first_sha = git(work, "rev-parse", "HEAD")
        git(work, "checkout", "-b", "feature")
        (work / "feature.py").write_text("ENABLED = True\n", "utf-8")
        git(work, "add", "--all")
        git(work, "commit", "-m", "feature")
        feature_sha = git(work, "rev-parse", "HEAD")
        git(work, "checkout", "main")
        git(work, "merge", "--no-ff", "feature", "-m", "merge feature")
        merge_sha = git(work, "rev-parse", "HEAD")
        git(work, "push", "origin", "main")

        engine = create_engine_from_url(f"sqlite+aiosqlite:///{tmp_path / 'catalog.db'}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            sessionmaker = create_sessionmaker(engine)
            async with sessionmaker() as session:
                repository = Repository(
                    canonical_name="h5vision/catalog-integration",
                    display_name="Catalog Integration",
                    provider="github",
                    remote_url="https://github.com/h5vision/catalog-integration.git",
                    default_branch_ref="refs/heads/main",
                )
                session.add(repository)
                await session.flush()
                branch = TrackedBranch(
                    repository_id=repository.repository_id,
                    branch_ref="refs/heads/main",
                    vss_project_id="catalog-integration--main",
                    current_head_sha=merge_sha,
                )
                session.add(branch)
                await session.commit()
                repository_id = repository.repository_id
                tracked_branch_id = branch.tracked_branch_id

            git_client = RepositoryGitClient(root=tmp_path / "snapshots")
            git_client.fetch_branch(
                repository_id=repository_id,
                tracked_branch_id=tracked_branch_id,
                remote_url=str(remote),
                branch_ref="refs/heads/main",
            )
            result = await CommitCatalogService(
                sessionmaker=sessionmaker,
                git_client=git_client,
                max_commits=100,
                batch_size=2,
                timeout_seconds=30,
                lease_seconds=300,
                subject_max_length=256,
            ).catalog_repository(repository_id)

            assert result.history_complete is True
            assert result.discovered_count == 3
            async with sessionmaker() as session:
                commits = {
                    item.commit_sha: item
                    for item in await session.scalars(select(RepositoryCommit))
                }
                assert set(commits) == {first_sha, feature_sha, merge_sha}
                merge_parents = list(
                    await session.scalars(
                        select(RepositoryCommitParent)
                        .where(
                            RepositoryCommitParent.repository_commit_id
                            == commits[merge_sha].repository_commit_id
                        )
                        .order_by(RepositoryCommitParent.parent_order)
                    )
                )
                assert [item.parent_sha for item in merge_parents] == [
                    first_sha,
                    feature_sha,
                ]
                assert await session.scalar(
                    select(func.count()).select_from(CommitCatalogRun)
                ) == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())
