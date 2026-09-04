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
from backend.features.repository_collection.use_cases.sync_tracked_branch import (
    SyncTrackedBranchUseCase,
)
from backend.infrastructure.database.models import Repository, TrackedBranch
from backend.ports.git import CommitGraphReader, RemoteObjectFetcher, RemoteRefReader


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
