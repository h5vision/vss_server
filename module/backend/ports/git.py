"""Git capability ports for repository observation, materialization, and comparison."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import UUID

from backend.features.commit_catalog.schemas import CommitGraphScanResult
from backend.features.repository_collection.git_client import GitCompareResult
from backend.features.repository_collection.schemas import RemoteBranchHead, RemoteTag


@runtime_checkable
class RemoteRefReader(Protocol):
    """Port for discovering remote references (branches, tags) without cloning."""

    def list_remote_heads(self, remote_url: str) -> list[RemoteBranchHead]:
        ...

    def list_remote_tags(self, remote_url: str, *, max_tags: int = 5_000) -> list[RemoteTag]:
        ...


@runtime_checkable
class RemoteObjectFetcher(Protocol):
    """Port for fetching exact Git objects into the cache repository."""

    def fetch_branch(
        self,
        remote_url: str,
        repository_id: UUID,
        branch_ref: str,
        expected_commit_sha: str,
    ) -> None:
        ...

    def fetch_tag(
        self,
        remote_url: str,
        repository_id: UUID,
        tag_ref: str,
        expected_commit_sha: str,
    ) -> None:
        ...

    def fetch_change_request_revisions(
        self,
        remote_url: str,
        repository_id: UUID,
        ref_map: dict[str, str],
    ) -> None:
        ...


@runtime_checkable
class CommitGraphReader(Protocol):
    """Port for inspecting commits and traversing ancestry relations."""

    def has_commit(self, repository_id: UUID, commit_sha: str) -> bool:
        ...

    def is_ancestor(self, repository_id: UUID, ancestor_sha: str, descendant_sha: str) -> bool:
        ...

    def scan_commit_graph(
        self,
        repository_id: UUID,
        from_commit_sha: str,
        *,
        max_commits: int = 500,
        batch_size: int = 100,
        known_commit_shas: set[str] | None = None,
    ) -> CommitGraphScanResult:
        ...


@runtime_checkable
class RevisionTreeMaterializer(Protocol):
    """Port for materializing and verifying an exact Git commit tree on disk."""

    def checkout_revision(
        self,
        repository_id: UUID,
        commit_sha: str,
        destination: Path,
    ) -> Path:
        ...

    def verify_checkout(self, destination: Path, expected_tree_sha: str) -> None:
        ...


@runtime_checkable
class RevisionComparator(Protocol):
    """Port for comparing two Git revisions and computing diff stats."""

    def compare_revisions(
        self,
        *,
        repository_id: UUID,
        base_revision: str,
        target_revision: str,
    ) -> GitCompareResult:
        ...


@runtime_checkable
class GitCapabilities(
    RemoteRefReader,
    RemoteObjectFetcher,
    CommitGraphReader,
    RevisionTreeMaterializer,
    RevisionComparator,
    Protocol,
):
    """Composite protocol combining all individual Git capability ports."""
    ...
