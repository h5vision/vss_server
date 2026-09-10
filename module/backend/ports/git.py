"""Git capability ports for repository observation, materialization, and comparison."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import UUID

from backend.features.commit_catalog.schemas import CommitGraphScanResult
from backend.features.repository_collection.schemas import RemoteBranchHead


@dataclass(frozen=True, slots=True)
class GitCompareFileChange:
    path: str
    change_type: str  # "added" | "modified" | "deleted" | "renamed" | "copied"
    old_path: str | None = None


@dataclass(frozen=True, slots=True)
class GitCompareResult:
    base_revision: str
    target_revision: str
    merge_base_revision: str | None
    ahead_count: int
    behind_count: int
    files_changed: int
    additions: int
    deletions: int
    changes: list[GitCompareFileChange]
    base_tree_sha: str | None = None
    target_tree_sha: str | None = None


@runtime_checkable
class RemoteRefReader(Protocol):
    """Port for discovering remote references (branches, tags) without cloning."""

    def list_remote_heads(self, remote_url: str) -> list[RemoteBranchHead]:
        ...


@runtime_checkable
class RemoteObjectFetcher(Protocol):
    """Port for fetching exact Git objects into the cache repository."""

    def fetch_branch(
        self,
        *,
        repository_id: UUID,
        tracked_branch_id: UUID,
        remote_url: str,
        branch_ref: str,
    ) -> str:
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
        *,
        repository_id: UUID,
        roots: list[str],
        max_commits: int = 500,
        timeout_seconds: float = 60.0,
        subject_max_length: int = 255,
    ) -> CommitGraphScanResult:
        ...


@runtime_checkable
class RevisionTreeMaterializer(Protocol):
    """Port for materializing and verifying an exact Git commit tree on disk."""

    def checkout_revision(
        self,
        *,
        repository_id: UUID,
        revision: str,
        destination: Path,
    ) -> Path:
        ...

    def verify_checkout(self, destination: Path, expected_revision: str) -> None:
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
        max_changes: int = 10_000,
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
