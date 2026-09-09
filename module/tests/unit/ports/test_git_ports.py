"""Unit tests verifying Git capability ports and RepositoryGitClient compatibility."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

from backend.features.repository_collection.git_client import (
    GitCompareResult,
    RepositoryGitClient,
)
from backend.ports.git import (
    CommitGraphReader,
    GitCapabilities,
    RemoteObjectFetcher,
    RemoteRefReader,
    RevisionComparator,
    RevisionTreeMaterializer,
)


def test_repository_git_client_implements_all_git_ports(tmp_path):
    client = RepositoryGitClient(root=tmp_path)

    assert isinstance(client, RemoteRefReader)
    assert isinstance(client, RemoteObjectFetcher)
    assert isinstance(client, CommitGraphReader)
    assert isinstance(client, RevisionTreeMaterializer)
    assert isinstance(client, RevisionComparator)
    assert isinstance(client, GitCapabilities)


def test_mock_can_satisfy_specific_port():
    comparator = MagicMock(spec=RevisionComparator)
    comparator.compare_revisions.return_value = GitCompareResult(
        base_revision="0" * 40,
        target_revision="1" * 40,
        merge_base_revision="0" * 40,
        ahead_count=1,
        behind_count=0,
        files_changed=1,
        additions=5,
        deletions=2,
        changes=[],
    )

    res = comparator.compare_revisions(
        repository_id=uuid4(),
        base_revision="0" * 40,
        target_revision="1" * 40,
    )
    assert res.files_changed == 1
    assert res.additions == 5
