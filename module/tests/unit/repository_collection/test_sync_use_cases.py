"""Unit tests for decomposed repository collection use cases."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from backend.features.repository_collection.schemas import RemoteBranchHead
from backend.features.repository_collection.store import RepositoryCollectionStore
from backend.features.repository_collection.use_cases.observe_repository import (
    ObserveRepositoryUseCase,
)
from backend.features.repository_collection.use_cases.orchestrate_sync import (
    SyncRepositoryUseCase,
)
from backend.features.repository_collection.use_cases.sync_tracked_branch import (
    SyncTrackedBranchUseCase,
)
from backend.infrastructure.database.models import Repository, RepositorySyncRun, TrackedBranch
from backend.ports.git import (
    CommitGraphReader,
    ManagedRepositoryWorkspace,
    RemoteObjectFetcher,
    RemoteRefReader,
)


@pytest.mark.anyio
async def test_observe_repository_use_case_catalog():
    repo_id = uuid4()
    mock_session = AsyncMock()
    mock_session.get.return_value = Repository(
        repository_id=repo_id,
        remote_url="https://example.com/repo.git",
        default_branch_ref="refs/heads/main",
        active=True,
    )
    sessionmaker = MagicMock()
    sessionmaker.return_value.__aenter__.return_value = mock_session

    ref_reader = MagicMock(spec=RemoteRefReader)
    ref_reader.list_remote_heads.return_value = [
        RemoteBranchHead(branch_ref="refs/heads/main", commit_sha="a" * 40),
        RemoteBranchHead(branch_ref="refs/heads/feature", commit_sha="b" * 40),
    ]

    use_case = ObserveRepositoryUseCase(sessionmaker=sessionmaker, ref_reader=ref_reader)
    result = await use_case.catalog_repository(repo_id)
    assert result.default_branch_exists is True
    assert len(result.branches) == 2


@pytest.mark.anyio
async def test_sync_tracked_branch_disabled_skips_fetch():
    sessionmaker = MagicMock()
    object_fetcher = MagicMock(spec=RemoteObjectFetcher)
    graph_reader = MagicMock(spec=CommitGraphReader)
    publisher = MagicMock()

    use_case = SyncTrackedBranchUseCase(
        sessionmaker=sessionmaker,
        object_fetcher=object_fetcher,
        graph_reader=graph_reader,
        publisher=publisher,
    )

    mock_session = AsyncMock()
    mock_store = AsyncMock(spec=RepositoryCollectionStore)
    mock_store.get_tracked_branch.return_value = TrackedBranch(
        tracked_branch_id=uuid4(),
        repository_id=uuid4(),
        branch_ref="refs/heads/main",
        vss_project_id="p1",
        tracked=False,
        current_head_sha="a" * 40,
    )

    # Use mock session context
    sessionmaker.return_value.__aenter__.return_value = mock_session
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "backend.features.repository_collection.use_cases.sync_tracked_branch.RepositoryCollectionStore",
            lambda s: mock_store,
        )
        outcome = await use_case.sync_branch(
            Repository(
                repository_id=uuid4(),
                remote_url="https://example.com/repo.git",
                default_branch_ref="refs/heads/main",
                active=True,
            ),
            tracked_branch_id=uuid4(),
            sync_run_id=uuid4(),
            lease_generation=1,
            request_id=uuid4(),
            remote_head="a" * 40,
        )

    assert outcome.ok is True
    assert outcome.reason == "TRACKED_BRANCH_DISABLED"
    object_fetcher.fetch_branch.assert_not_called()


@pytest.mark.anyio
async def test_sync_repository_ensures_managed_workspace_before_remote_observation(monkeypatch):
    repository_id = uuid4()
    repository = Repository(
        repository_id=repository_id,
        canonical_name="h5vision/vss_server",
        display_name="vss_server",
        provider="github",
        remote_url="https://example.com/vss_server.git",
        default_branch_ref="refs/heads/main",
        active=True,
    )
    sync_run = RepositorySyncRun(
        sync_run_id=uuid4(),
        request_id=uuid4(),
        repository_id=repository_id,
        trigger="manual",
        state="running",
        reason="COLLECTION_SYNC_RUNNING",
        detail="running",
        lease_generation=1,
    )
    workspace_manager = MagicMock(spec=ManagedRepositoryWorkspace)
    ref_reader = MagicMock(spec=RemoteRefReader)
    ref_reader.list_remote_heads.return_value = []
    use_case = SyncRepositoryUseCase(
        sessionmaker=MagicMock(),
        ref_reader=ref_reader,
        sync_branch_use_case=MagicMock(spec=SyncTrackedBranchUseCase),
        workspace_manager=workspace_manager,
    )

    monkeypatch.setattr(
        SyncRepositoryUseCase,
        "_claim_sync",
        AsyncMock(return_value=(repository, sync_run)),
    )
    monkeypatch.setattr(
        SyncRepositoryUseCase,
        "_refresh_lease",
        AsyncMock(side_effect=[2, 3, 4]),
    )
    monkeypatch.setattr(
        SyncRepositoryUseCase,
        "_tracked_branch_ids",
        AsyncMock(return_value=[]),
    )
    finish_result = MagicMock()
    monkeypatch.setattr(
        SyncRepositoryUseCase,
        "_finish_run",
        AsyncMock(return_value=finish_result),
    )

    result = await use_case.sync_repository(repository_id)

    assert result is finish_result
    workspace_manager.ensure_repository.assert_called_once_with(
        repository_id=repository_id,
        canonical_name="h5vision/vss_server",
        remote_url="https://example.com/vss_server.git",
        default_branch_ref="refs/heads/main",
    )
    ref_reader.list_remote_heads.assert_called_once_with(repository.remote_url)
