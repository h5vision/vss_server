"""Unit tests for Admin Use Cases (CompareRevisionsUseCase, MaterializeCommitUseCase)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from backend.features.admin.schemas import (
    AdminCommitCompareResponse,
    AdminCommitMaterializeResponse,
)
from backend.features.admin.use_cases.compare_revisions import CompareRevisionsUseCase
from backend.features.admin.use_cases.materialize_commit import MaterializeCommitUseCase
from backend.features.repository_collection.git_client import GitCompareResult
from backend.infrastructure.database.models import Repository


@pytest.mark.anyio
async def test_compare_revisions_use_case_happy_path():
    repo_id = uuid4()
    req_id = uuid4()
    base_rev = "1" * 40
    target_rev = "2" * 40

    mock_session = AsyncMock()
    fake_repo = Repository(
        repository_id=repo_id,
        canonical_name="h5vision/vision",
        remote_url="https://github.com/h5vision/vision.git",
        default_branch_ref="refs/heads/main",
    )
    mock_session.get.return_value = fake_repo
    mock_session.scalar.return_value = None

    mock_comparator = MagicMock()
    mock_comparator.compare_revisions.return_value = GitCompareResult(
        base_revision=base_rev,
        target_revision=target_rev,
        merge_base_revision="0" * 40,
        ahead_count=1,
        behind_count=0,
        files_changed=2,
        additions=10,
        deletions=3,
        changes=[],
    )

    use_case = CompareRevisionsUseCase(comparator=mock_comparator, session=mock_session)
    result: AdminCommitCompareResponse = await use_case.execute(
        repository_id=repo_id,
        base_revision=base_rev,
        target_revision=target_rev,
        actor_id="operator-tester",
        request_id=req_id,
    )

    assert result.ok is True
    assert result.repository_id == repo_id
    assert result.files_changed == 2
    assert result.additions == 10
    assert result.deletions == 3
    mock_comparator.compare_revisions.assert_called_once_with(
        repository_id=repo_id,
        base_revision=base_rev,
        target_revision=target_rev,
    )


@pytest.mark.anyio
async def test_materialize_commit_use_case_happy_path(monkeypatch):
    repo_id = uuid4()
    req_id = uuid4()
    commit_sha = "3" * 40
    snap_id = uuid4()

    mock_session = AsyncMock()
    mock_git_client = MagicMock()
    mock_materializer = MagicMock()

    # Mock AdminStore.materialize_commit
    fake_response = AdminCommitMaterializeResponse(
        ok=True,
        repository_id=repo_id,
        commit_sha=commit_sha,
        vss_project_id="vision--module",
        snapshot_id=snap_id,
        created=True,
        state="materialized",
        materialized_locator=f"revision:{commit_sha}",
    )

    async def fake_materialize_commit(*args, **kwargs):
        return fake_response

    monkeypatch.setattr(
        "backend.features.admin.use_cases.materialize_commit.AdminStore.materialize_commit",
        fake_materialize_commit,
    )

    use_case = MaterializeCommitUseCase(
        materializer=mock_materializer,
        git_client=mock_git_client,
        session=mock_session,
    )

    result = await use_case.execute(
        repository_id=repo_id,
        commit_sha=commit_sha,
        actor_id="operator-tester",
        request_id=req_id,
    )

    assert result.ok is True
    assert result.snapshot_id == snap_id
    assert result.created is True
