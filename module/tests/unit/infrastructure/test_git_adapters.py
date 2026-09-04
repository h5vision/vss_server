"""Unit tests verifying physical Git capability adapters and Facade composition."""

from __future__ import annotations

from inspect import signature
from pathlib import Path

from backend.features.repository_collection.git_client import RepositoryGitClient
from backend.infrastructure.git import (
    GitCacheLayout,
    GitCommandRunner,
    GitCommitGraphAdapter,
    GitRemoteObjectAdapter,
    GitRemoteRefAdapter,
    GitRevisionCompareAdapter,
    GitTreeCheckoutAdapter,
)
from backend.ports.git import (
    CommitGraphReader,
    GitCapabilities,
    RemoteObjectFetcher,
    RemoteRefReader,
    RevisionComparator,
    RevisionTreeMaterializer,
)


def test_git_adapters_satisfy_their_respective_ports(tmp_path: Path):
    layout = GitCacheLayout(root=tmp_path)
    runner = GitCommandRunner()

    refs_adapter = GitRemoteRefAdapter(runner=runner)
    assert isinstance(refs_adapter, RemoteRefReader)

    objects_adapter = GitRemoteObjectAdapter(layout=layout, runner=runner)
    assert isinstance(objects_adapter, RemoteObjectFetcher)

    graph_adapter = GitCommitGraphAdapter(layout=layout, runner=runner)
    assert isinstance(graph_adapter, CommitGraphReader)

    checkout_adapter = GitTreeCheckoutAdapter(layout=layout, runner=runner)
    assert isinstance(checkout_adapter, RevisionTreeMaterializer)

    compare_adapter = GitRevisionCompareAdapter(layout=layout, runner=runner)
    assert isinstance(compare_adapter, RevisionComparator)


def test_repository_git_client_facade_composition(tmp_path: Path):
    client = RepositoryGitClient(root=tmp_path)

    assert isinstance(client, GitCapabilities)
    assert isinstance(client, RemoteRefReader)
    assert isinstance(client, RemoteObjectFetcher)
    assert isinstance(client, CommitGraphReader)
    assert isinstance(client, RevisionTreeMaterializer)
    assert isinstance(client, RevisionComparator)

    # Verify adapter property wiring
    assert isinstance(client._refs, GitRemoteRefAdapter)
    assert isinstance(client._objects, GitRemoteObjectAdapter)
    assert isinstance(client._graph, GitCommitGraphAdapter)
    assert isinstance(client._checkout, GitTreeCheckoutAdapter)
    assert isinstance(client._compare, GitRevisionCompareAdapter)
    assert isinstance(client._layout, GitCacheLayout)


def test_repository_git_client_capability_signatures_match_port_keywords():
    checkout_signature = signature(RepositoryGitClient.checkout_revision)
    assert checkout_signature.return_annotation in {Path, "Path"}

    verify_parameters = list(signature(RepositoryGitClient.verify_checkout).parameters)
    assert verify_parameters == ["self", "destination", "expected_revision"]

    ancestor_parameters = list(signature(RepositoryGitClient.is_ancestor).parameters)
    assert ancestor_parameters == [
        "self",
        "repository_id",
        "ancestor_sha",
        "descendant_sha",
    ]
