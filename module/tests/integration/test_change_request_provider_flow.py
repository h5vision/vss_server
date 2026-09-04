"""GitHub provider metadata to verified PR ref and commit catalog E2E."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import httpx2
from sqlalchemy import func, select

from backend.features.change_requests.service import ChangeRequestCollectionService
from backend.features.commit_catalog.service import CommitCatalogService
from backend.features.repository_collection.git_client import RepositoryGitClient
from backend.features.repository_collection.materializer import CollectedRevisionMaterializer
from backend.features.repository_collection.publisher import CollectedSnapshotPublisher
from backend.features.repository_collection.schemas import TrackedBranchCreateRequest
from backend.features.repository_collection.service import RepositoryCollectionService
from backend.infrastructure.database.base import Base
from backend.infrastructure.database.engine import create_engine_from_url, create_sessionmaker
from backend.infrastructure.database.models import (
    ChangeRequest,
    ChangeRequestRevision,
    Repository,
    RepositoryCommit,
)
from backend.integrations.change_requests.github import GitHubChangeRequestClient
from backend.integrations.vss.client import VssHttpClient


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def test_repository_sync_collects_github_pr_and_catalogs_disconnected_head(
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    remote.mkdir()
    work.mkdir()
    git(remote, "init", "--bare")
    git(work, "init", "-b", "main")
    git(work, "config", "user.email", "provider@example.invalid")
    git(work, "config", "user.name", "Provider Flow")
    git(work, "remote", "add", "origin", str(remote))
    (work / "main.py").write_text("VERSION = 1\n", "utf-8")
    git(work, "add", "--all")
    git(work, "commit", "-m", "base")
    base_sha = git(work, "rev-parse", "HEAD")
    git(work, "push", "origin", "main")
    git(work, "checkout", "-b", "feature/context")
    (work / "context.py").write_text("ENABLED = True\n", "utf-8")
    git(work, "add", "--all")
    git(work, "commit", "-m", "add context")
    head_sha = git(work, "rev-parse", "HEAD")
    git(work, "push", "origin", f"{head_sha}:refs/pull/42/head")
    git(work, "checkout", "main")

    provider_calls = 0

    def provider_transport(request: httpx2.Request) -> httpx2.Response:
        nonlocal provider_calls
        provider_calls += 1
        assert request.url.path == "/repos/h5vision/provider-flow/pulls"
        return httpx2.Response(
            200,
            json=[
                {
                    "number": 42,
                    "title": "Add context",
                    "state": "open",
                    "updated_at": "2026-09-03T01:00:00Z",
                    "merged_at": None,
                    "merge_commit_sha": "9" * 40,
                    "base": {"ref": "main", "sha": base_sha},
                    "head": {"ref": "feature/context", "sha": head_sha},
                }
            ],
        )

    vss_calls: list[dict] = []

    def vss_transport(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        vss_calls.append(body)
        return httpx2.Response(
            202,
            json={
                "accepted": True,
                "project_id": body["project_id"],
                "state": "running",
            },
        )

    async def scenario() -> None:
        engine = create_engine_from_url(f"sqlite+aiosqlite:///{tmp_path / 'provider.db'}")
        github_client = GitHubChangeRequestClient(
            base_url="https://api.github.com",
            token="provider-secret",
            api_version="2026-03-10",
            max_pages=2,
            transport=httpx2.MockTransport(provider_transport),
        )
        vss_client = VssHttpClient(
            base_url="http://vss.example:8200",
            transport=httpx2.MockTransport(vss_transport),
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            sessionmaker = create_sessionmaker(engine)
            async with sessionmaker() as session:
                repository = Repository(
                    canonical_name="h5vision/provider-flow",
                    display_name="Provider Flow",
                    provider="github",
                    remote_url=str(remote),
                    default_branch_ref="refs/heads/main",
                )
                session.add(repository)
                await session.commit()
                repository_id = repository.repository_id

            git_client = RepositoryGitClient(root=tmp_path / "snapshots")
            change_request_service = ChangeRequestCollectionService(
                sessionmaker=sessionmaker,
                git_client=git_client,
                provider_clients={"github": github_client},
            )
            commit_catalog_service = CommitCatalogService(
                sessionmaker=sessionmaker,
                git_client=git_client,
                max_commits=100,
                batch_size=10,
                timeout_seconds=30,
                lease_seconds=300,
                subject_max_length=256,
            )
            service = RepositoryCollectionService(
                sessionmaker=sessionmaker,
                git_client=git_client,
                publisher=CollectedSnapshotPublisher(
                    sessionmaker=sessionmaker,
                    materializer=CollectedRevisionMaterializer(
                        root=tmp_path / "snapshots",
                        git_client=git_client,
                    ),
                    vss_client=vss_client,
                ),
                change_request_service=change_request_service,
                commit_catalog_service=commit_catalog_service,
            )
            await service.register_tracked_branch(
                TrackedBranchCreateRequest(
                    repository_id=repository_id,
                    branch_ref="refs/heads/main",
                    vss_project_id="provider-flow--main",
                )
            )
            result = await service.sync_repository(repository_id)

            assert result.ok is True
            assert provider_calls == 1
            assert len(vss_calls) == 1
            async with sessionmaker() as session:
                assert await session.scalar(
                    select(func.count()).select_from(ChangeRequest)
                ) == 1
                assert await session.scalar(
                    select(func.count()).select_from(ChangeRequestRevision)
                ) == 1
                commits = set(await session.scalars(select(RepositoryCommit.commit_sha)))
                assert commits == {base_sha, head_sha}
        finally:
            github_client.close()
            vss_client.close()
            await engine.dispose()

    asyncio.run(scenario())
