"""선택 Branch HEAD 수집부터 immutable Snapshot/VSS 제출까지의 통합 흐름."""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx2
from sqlalchemy import func, select

from backend.features.commit_catalog.service import CommitCatalogService
from backend.features.indexing.index import SnapshotIndexService
from backend.features.materialization.service import SnapshotMaterializer
from backend.features.materialization.source import GitTreeSource
from backend.features.repository_collection.git_client import RepositoryGitClient
from backend.features.repository_collection.materializer import CollectedRevisionMaterializer
from backend.features.repository_collection.publisher import CollectedSnapshotPublisher
from backend.features.repository_collection.schemas import TrackedBranchCreateRequest
from backend.features.repository_collection.service import RepositoryCollectionService
from backend.infrastructure.database.base import Base
from backend.infrastructure.database.engine import create_engine_from_url, create_sessionmaker
from backend.infrastructure.database.models import (
    BranchHeadHistory,
    CommitCatalogRun,
    Repository,
    RepositoryCommit,
    RepositorySyncRun,
    Snapshot,
    TrackedBranch,
)
from backend.integrations.vss.client import VssHttpClient


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def create_remote(root: Path) -> tuple[Path, Path, str]:
    remote = root / "remote.git"
    work = root / "work"
    remote.mkdir()
    work.mkdir()
    git(remote, "init", "--bare")
    git(work, "init", "-b", "main")
    git(work, "config", "user.email", "collector@example.invalid")
    git(work, "config", "user.name", "Collector Test")
    git(work, "remote", "add", "origin", str(remote))
    (work / "app.py").write_text("version = 1\n", "utf-8")
    git(work, "add", "--all")
    git(work, "commit", "-m", "initial")
    initial_sha = git(work, "rev-parse", "HEAD")
    git(work, "push", "-u", "origin", "main")

    git(work, "checkout", "-b", "feature")
    (work / "feature.py").write_text("feature = True\n", "utf-8")
    git(work, "add", "--all")
    git(work, "commit", "-m", "feature")
    git(work, "push", "-u", "origin", "feature")
    git(work, "checkout", "main")
    return remote, work, initial_sha


def test_selected_branch_history_materialization_and_vss_submission(tmp_path: Path) -> None:
    remote, work, initial_sha = create_remote(tmp_path)
    database_path = tmp_path / "collection.db"
    materialization_root = tmp_path / "snapshots"
    vss_calls: list[dict] = []

    def fake_vss(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "POST"
        assert request.url.path == "/index"
        body = json.loads(request.content)
        vss_calls.append(body)
        project_root = Path(body["project_root"])
        assert git(project_root, "rev-parse", "HEAD") in {
            initial_sha,
            git(work, "rev-parse", "HEAD"),
        }
        assert git(project_root, "status", "--porcelain=v1", "--untracked-files=all") == ""
        assert body["project_id"] == "collection--main"
        assert "revision" not in body
        return httpx2.Response(
            202,
            json={
                "accepted": True,
                "project_id": "collection--main",
                "state": "running",
                "fingerprint": {"use_bm25": True},
            },
        )

    async def scenario() -> None:
        engine = create_engine_from_url(f"sqlite+aiosqlite:///{database_path}")
        vss_client = VssHttpClient(
            base_url="http://vss.example:8200",
            transport=httpx2.MockTransport(fake_vss),
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            sessionmaker = create_sessionmaker(engine)
            async with sessionmaker() as session:
                repository = Repository(
                    canonical_name="h5vision/collection",
                    display_name="Collection",
                    provider="git",
                    remote_url=str(remote),
                    default_branch_ref="refs/heads/main",
                )
                session.add(repository)
                await session.commit()
                repository_id = repository.repository_id

            git_client = RepositoryGitClient(root=materialization_root)
            publisher = CollectedSnapshotPublisher(
                sessionmaker=sessionmaker,
                materializer=CollectedRevisionMaterializer(
                    root=materialization_root,
                    git_client=git_client,
                ),
            )
            commit_catalog_service = CommitCatalogService(
                sessionmaker=sessionmaker,
                git_client=git_client,
                max_commits=100,
                batch_size=2,
                timeout_seconds=30,
                lease_seconds=300,
                subject_max_length=256,
            )
            service = RepositoryCollectionService(
                sessionmaker=sessionmaker,
                git_client=git_client,
                publisher=publisher,
                commit_catalog_service=commit_catalog_service,
            )

            catalog = await service.catalog_repository(repository_id)
            assert catalog.default_branch_exists is True
            assert {item.branch_ref for item in catalog.branches} == {
                "refs/heads/main",
                "refs/heads/feature",
            }
            tracked = await service.register_tracked_branch(
                TrackedBranchCreateRequest(
                    repository_id=repository_id,
                    branch_ref="refs/heads/main",
                    vss_project_id="collection--main",
                )
            )

            first = await service.sync_repository(repository_id)
            assert first.ok is True
            assert first.outcomes[0].change_type == "created"
            assert first.outcomes[0].observed_head_sha == initial_sha
            assert first.outcomes[0].reason == "SNAPSHOT_MATERIALIZED"
            assert len(vss_calls) == 0

            unchanged = await service.sync_repository(repository_id, trigger="periodic")
            assert unchanged.ok is True
            assert unchanged.outcomes[0].reason == "SNAPSHOT_ALREADY_MATERIALIZED"
            assert len(vss_calls) == 0

            (work / "app.py").write_text("version = 2\n", "utf-8")
            git(work, "add", "--all")
            git(work, "commit", "-m", "fast forward")
            second_sha = git(work, "rev-parse", "HEAD")
            git(work, "push", "origin", "main")
            advanced = await service.sync_repository(repository_id)
            assert advanced.ok is True
            assert advanced.outcomes[0].change_type == "fast_forward"
            assert advanced.outcomes[0].observed_head_sha == second_sha
            assert len(vss_calls) == 0

            git(work, "checkout", "--orphan", "replacement")
            git(work, "rm", "-rf", ".")
            (work / "replacement.py").write_text("replacement = True\n", "utf-8")
            git(work, "add", "--all")
            git(work, "commit", "-m", "replacement history")
            replacement_sha = git(work, "rev-parse", "HEAD")
            git(work, "push", "--force", "origin", "HEAD:main")
            rewound = await service.sync_repository(repository_id)
            assert rewound.ok is True
            assert rewound.outcomes[0].change_type == "rewind"
            assert rewound.outcomes[0].observed_head_sha == replacement_sha
            assert len(vss_calls) == 0

            git(remote, "update-ref", "-d", "refs/heads/main")
            deleted = await service.sync_repository(repository_id)
            assert deleted.ok is True
            assert deleted.outcomes[0].reason == "BRANCH_DELETED"
            assert deleted.outcomes[0].change_type == "deleted"
            assert len(vss_calls) == 0

            (work / "replacement.py").write_text("replacement = 'recreated'\n", "utf-8")
            git(work, "add", "--all")
            git(work, "commit", "-m", "recreated branch")
            recreated_sha = git(work, "rev-parse", "HEAD")
            git(work, "push", "origin", "HEAD:main")
            recreated = await service.sync_repository(repository_id)
            assert recreated.ok is True
            assert recreated.outcomes[0].change_type == "recreated"
            assert recreated.outcomes[0].observed_head_sha == recreated_sha
            assert len(vss_calls) == 0

            async with sessionmaker() as session:
                history = list(
                    await session.scalars(
                        select(BranchHeadHistory).order_by(BranchHeadHistory.observed_at)
                    )
                )
                assert [item.change_type for item in history] == [
                    "created",
                    "fast_forward",
                    "rewind",
                    "deleted",
                    "recreated",
                ]
                assert await session.scalar(select(func.count()).select_from(Snapshot)) == 4
                assert await session.scalar(
                    select(func.count()).select_from(RepositoryCommit)
                ) >= 4
                assert await session.scalar(
                    select(func.count()).select_from(CommitCatalogRun)
                ) == 6
                assert await session.scalar(select(func.count()).select_from(TrackedBranch)) == 1
                sync_run_count = await session.scalar(
                    select(func.count()).select_from(RepositorySyncRun)
                )
                assert sync_run_count == 6
                stored_branch = await session.get(TrackedBranch, tracked.tracked_branch_id)
                assert stored_branch.current_head_sha == recreated_sha
                assert all(
                    item.tracked_branch_id == tracked.tracked_branch_id for item in history
                )
        finally:
            vss_client.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_same_head_resumes_unfinished_collector_snapshot(tmp_path: Path) -> None:
    remote, _, initial_sha = create_remote(tmp_path)
    database_path = tmp_path / "resume.db"
    materialization_root = tmp_path / "snapshots"
    calls: list[dict] = []

    def fake_vss(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        calls.append(body)
        assert git(Path(body["project_root"]), "rev-parse", "HEAD") == initial_sha
        return httpx2.Response(
            202,
            json={
                "accepted": True,
                "project_id": "resume--main",
                "state": "running",
            },
        )

    async def scenario() -> None:
        engine = create_engine_from_url(f"sqlite+aiosqlite:///{database_path}")
        vss_client = VssHttpClient(
            base_url="http://vss.example:8200",
            transport=httpx2.MockTransport(fake_vss),
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            sessionmaker = create_sessionmaker(engine)
            async with sessionmaker() as session:
                repository = Repository(
                    canonical_name="h5vision/resume",
                    display_name="Resume",
                    provider="git",
                    remote_url=str(remote),
                    default_branch_ref="refs/heads/main",
                )
                session.add(repository)
                await session.flush()
                branch = TrackedBranch(
                    repository_id=repository.repository_id,
                    branch_ref="refs/heads/main",
                    vss_project_id="resume--main",
                    current_head_sha=initial_sha,
                )
                run = RepositorySyncRun(
                    request_id=uuid4(),
                    repository_id=repository.repository_id,
                    trigger="manual",
                    state="succeeded",
                    reason="COLLECTION_SYNC_COMPLETED",
                    detail="이전 HEAD 관측 완료",
                    retryable=False,
                    lease_expires_at=datetime.now(timezone.utc),
                    finished_at=datetime.now(timezone.utc),
                )
                session.add_all([branch, run])
                await session.flush()
                session.add(
                    BranchHeadHistory(
                        tracked_branch_id=branch.tracked_branch_id,
                        sync_run_id=run.sync_run_id,
                        previous_head_sha=None,
                        observed_head_sha=initial_sha,
                        change_type="created",
                    )
                )
                snapshot = Snapshot(
                    request_id=uuid4(),
                    binding_id=None,
                    tracked_branch_id=branch.tracked_branch_id,
                    frontend_project_id=None,
                    repository_id=repository.repository_id,
                    branch_ref=branch.branch_ref,
                    vss_project_id=branch.vss_project_id,
                    base_revision=initial_sha,
                    target_revision=initial_sha,
                    source_type="remote_clone",
                    state="materializing",
                )
                session.add(snapshot)
                await session.commit()
                repository_id = repository.repository_id
                tracked_branch_id = branch.tracked_branch_id
                snapshot_id = snapshot.snapshot_id

            stale_staging = (
                materialization_root
                / tracked_branch_id.hex
                / "staging"
                / str(snapshot_id)
            )
            stale_staging.mkdir(parents=True)
            (stale_staging / "partial.txt").write_text("partial", "utf-8")

            git_client = RepositoryGitClient(root=materialization_root)
            service = RepositoryCollectionService(
                sessionmaker=sessionmaker,
                git_client=git_client,
                publisher=CollectedSnapshotPublisher(
                    sessionmaker=sessionmaker,
                    materializer=CollectedRevisionMaterializer(
                        root=materialization_root,
                        git_client=git_client,
                    ),
                ),
            )
            result = await service.sync_repository(repository_id)
            assert result.ok is True
            assert result.outcomes[0].reason == "SNAPSHOT_MATERIALIZED"
            assert result.outcomes[0].change_type is None
            assert len(calls) == 0
            assert not stale_staging.exists()

            async with sessionmaker() as session:
                saved = await session.get(Snapshot, snapshot_id)
                assert saved.state == "materialized"
                assert saved.attempt_count == 0
                assert await session.scalar(select(func.count()).select_from(Snapshot)) == 1
                history_count = await session.scalar(
                    select(func.count()).select_from(BranchHeadHistory)
                )
                assert history_count == 1
        finally:
            vss_client.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_collected_snapshot_requires_explicit_admin_index_for_vss_submission(
    tmp_path: Path,
) -> None:
    remote, _, initial_sha = create_remote(tmp_path)
    database_path = tmp_path / "collection-admin-index.db"
    materialization_root = tmp_path / "snapshots"
    vss_calls: list[dict] = []
    seen_paths: list[str] = []

    def fake_vss(request: httpx2.Request) -> httpx2.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/index/status":
            return httpx2.Response(
                200,
                json={"project_id": "collection-index--main", "state": "none"},
            )
        if request.url.path == "/index/exists":
            return httpx2.Response(
                200,
                json={"project_id": "collection-index--main", "exists": False},
            )
        if request.url.path == "/index":
            body = json.loads(request.content)
            vss_calls.append(body)
            project_root = Path(body["project_root"])
            assert git(project_root, "rev-parse", "HEAD") == initial_sha
            assert git(
                project_root,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ) == ""
            return httpx2.Response(
                202,
                json={
                    "accepted": True,
                    "project_id": "collection-index--main",
                    "state": "running",
                },
            )
        raise AssertionError(f"unexpected VSS path: {request.url.path}")

    async def scenario() -> None:
        engine = create_engine_from_url(f"sqlite+aiosqlite:///{database_path}")
        client = VssHttpClient(
            base_url="http://vss.example:8200",
            transport=httpx2.MockTransport(fake_vss),
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            sessionmaker = create_sessionmaker(engine)
            async with sessionmaker() as session:
                repository = Repository(
                    canonical_name="h5vision/collection-index",
                    display_name="Collection Index",
                    provider="git",
                    remote_url=str(remote),
                    default_branch_ref="refs/heads/main",
                )
                session.add(repository)
                await session.commit()
                repository_id = repository.repository_id

            git_client = RepositoryGitClient(root=materialization_root)
            collection_service = RepositoryCollectionService(
                sessionmaker=sessionmaker,
                git_client=git_client,
                publisher=CollectedSnapshotPublisher(
                    sessionmaker=sessionmaker,
                    materializer=CollectedRevisionMaterializer(
                        root=materialization_root,
                        git_client=git_client,
                    ),
                ),
            )
            await collection_service.register_tracked_branch(
                TrackedBranchCreateRequest(
                    repository_id=repository_id,
                    branch_ref="refs/heads/main",
                    vss_project_id="collection-index--main",
                )
            )
            synced = await collection_service.sync_repository(repository_id)
            assert synced.ok is True
            assert synced.outcomes[0].reason == "SNAPSHOT_MATERIALIZED"
            assert seen_paths == []

            async with sessionmaker() as session:
                snapshot = await session.scalar(select(Snapshot))
                assert snapshot is not None
                assert snapshot.state == "materialized"
                assert snapshot.attempt_count == 0
                snapshot_id = snapshot.snapshot_id

            indexed = await SnapshotIndexService(
                sessionmaker=sessionmaker,
                materializer=SnapshotMaterializer(
                    root=materialization_root,
                    source=GitTreeSource(command_timeout_seconds=10),
                ),
                vss_client=client,
            ).index(snapshot_id, request_id=uuid4())
            assert indexed.status_code == 202
            assert indexed.body.reason == "VSS_INDEX_ACCEPTED"
            assert indexed.body.attempt_count == 1
        finally:
            client.close()
            await engine.dispose()

    asyncio.run(scenario())

    assert seen_paths == ["/index/status", "/index/exists", "/index"]
    assert len(vss_calls) == 1
    assert vss_calls[0]["project_id"] == "collection-index--main"
    assert vss_calls[0]["force"] is False
    assert "remote" not in vss_calls[0]
    assert "revision" not in vss_calls[0]
