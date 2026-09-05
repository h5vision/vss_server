"""원격 Branch 조회와 선택 Branch object만 보존하는 Git cache client (Facade)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from backend.features.commit_catalog.schemas import CommitGraphScanResult
from backend.features.repository_collection.errors import CollectionError
from backend.features.repository_collection.schemas import RemoteBranchHead, RemoteTag
from backend.infrastructure.git import (
    GitCacheLayout,
    GitCommandRunner,
    GitCommitGraphAdapter,
    GitCompareFileChange,
    GitCompareResult,
    GitRemoteObjectAdapter,
    GitRemoteRefAdapter,
    GitRevisionCompareAdapter,
    GitTreeCheckoutAdapter,
    assert_inside_root,
    is_link_or_junction,
    is_sha,
    remove_readonly,
)
from backend.ports.git import GitCapabilities

_remove_readonly = remove_readonly
_is_link_or_junction = is_link_or_junction


@dataclass(frozen=True, slots=True)
class RepositoryGitClient(GitCapabilities):
    """Git credential과 stderr를 외부 계약에 노출하지 않는 동기식 Git 경계 Facade."""

    root: Path
    command_timeout_seconds: float = 60.0
    runner: GitCommandRunner = field(default_factory=GitCommandRunner)

    @property
    def _layout(self) -> GitCacheLayout:
        return GitCacheLayout(root=self.root)

    @property
    def _refs(self) -> GitRemoteRefAdapter:
        return GitRemoteRefAdapter(runner=self.runner)

    @property
    def _objects(self) -> GitRemoteObjectAdapter:
        return GitRemoteObjectAdapter(layout=self._layout, runner=self.runner)

    @property
    def _graph(self) -> GitCommitGraphAdapter:
        return GitCommitGraphAdapter(layout=self._layout, runner=self.runner)

    @property
    def _checkout(self) -> GitTreeCheckoutAdapter:
        return GitTreeCheckoutAdapter(layout=self._layout, runner=self.runner)

    @property
    def _compare(self) -> GitRevisionCompareAdapter:
        return GitRevisionCompareAdapter(layout=self._layout, runner=self.runner)

    # --- RemoteRefReader ---
    def list_remote_heads(self, remote_url: str) -> list[RemoteBranchHead]:
        return self._refs.list_remote_heads(remote_url)

    def list_remote_tags(self, remote_url: str, *, max_tags: int = 5_000) -> list[RemoteTag]:
        return self._refs.list_remote_tags(remote_url, max_tags=max_tags)

    # --- RemoteObjectFetcher ---
    def fetch_branch(
        self,
        *,
        repository_id: UUID,
        tracked_branch_id: UUID,
        remote_url: str,
        branch_ref: str,
    ) -> str:
        return self._objects.fetch_branch(
            repository_id=repository_id,
            tracked_branch_id=tracked_branch_id,
            remote_url=remote_url,
            branch_ref=branch_ref,
        )

    def fetch_tag(
        self,
        *,
        repository_id: UUID,
        remote_url: str,
        tag_ref: str,
        expected_commit_sha: str,
    ) -> None:
        self._objects.fetch_tag(
            repository_id=repository_id,
            remote_url=remote_url,
            tag_ref=tag_ref,
            expected_commit_sha=expected_commit_sha,
        )

    def fetch_change_request_revisions(
        self,
        *,
        repository_id: UUID,
        remote_url: str,
        provider: str,
        external_number: int,
        base_ref: str,
        base_sha: str,
        head_sha: str,
        merge_sha: str | None = None,
    ) -> None:
        self._objects.fetch_change_request_revisions(
            repository_id=repository_id,
            remote_url=remote_url,
            provider=provider,
            external_number=external_number,
            base_ref=base_ref,
            base_sha=base_sha,
            head_sha=head_sha,
            merge_sha=merge_sha,
        )

    # --- CommitGraphReader ---
    def has_commit(self, repository_id: UUID, commit_sha: str) -> bool:
        return self._graph.has_commit(repository_id, commit_sha)

    def is_ancestor(
        self,
        repository_id: UUID,
        ancestor_sha: str,
        descendant_sha: str,
    ) -> bool:
        return self._graph.is_ancestor(repository_id, ancestor_sha, descendant_sha)

    def scan_commit_graph(
        self,
        *,
        repository_id: UUID,
        roots: list[str],
        max_commits: int = 500,
        timeout_seconds: float = 60.0,
        subject_max_length: int = 255,
    ) -> CommitGraphScanResult:
        return self._graph.scan_commit_graph(
            repository_id=repository_id,
            roots=roots,
            max_commits=max_commits,
            timeout_seconds=timeout_seconds,
            subject_max_length=subject_max_length,
        )

    # --- RevisionTreeMaterializer ---
    def checkout_revision(
        self,
        *,
        repository_id: UUID,
        revision: str,
        destination: Path,
    ) -> Path:
        return self._checkout.checkout_revision(
            repository_id=repository_id,
            revision=revision,
            destination=destination,
        )

    def verify_checkout(self, destination: Path, expected_revision: str) -> None:
        self._checkout.verify_checkout(destination, expected_revision)

    # --- RevisionComparator ---
    def compare_revisions(
        self,
        *,
        repository_id: UUID,
        base_revision: str,
        target_revision: str,
        max_changes: int = 10_000,
    ) -> GitCompareResult:
        return self._compare.compare_revisions(
            repository_id=repository_id,
            base_revision=base_revision,
            target_revision=target_revision,
            max_changes=max_changes,
        )

    # --- Backward compatibility helpers for internal callers/tests ---
    def _cache_path(self, repository_id: UUID) -> Path:
        return self._layout.cache_path(repository_id)

    @property
    def _cache_root(self) -> Path:
        return self._layout.cache_root

    def _ensure_cache(self, repository_id: UUID, remote_url: str | None = None) -> Path:
        return self._layout.ensure_cache(repository_id, self.runner, remote_url=remote_url)

    def _assert_cache_path_safe(self, path: Path) -> None:
        self._layout.assert_safe(path)

    def _output(self, command: list[str], *, failure: Exception) -> str:
        return self.runner.output(
            command,
            timeout_seconds=self.command_timeout_seconds,
            failure=failure,
        )

    def _run(
        self,
        command: list[str],
        *,
        failure: Exception,
        allowed_returncodes: set[int] | None = None,
        timeout_seconds: float | None = None,
        input_text: str | None = None,
    ) -> Any:
        return self.runner.run(
            command,
            timeout_seconds=timeout_seconds or self.command_timeout_seconds,
            allowed_returncodes=allowed_returncodes,
            input_text=input_text,
            failure=failure,
        )

    @staticmethod
    def _is_sha(value: str) -> bool:
        return is_sha(value)

    @staticmethod
    def _cache_failure() -> CollectionError:
        return CollectionError(
            reason="REPOSITORY_CACHE_FAILED",
            detail="Repository Git cache를 안전하게 준비하지 못했습니다.",
            retryable=True,
            status_code=500,
        )


__all__ = [
    "GitCompareFileChange",
    "GitCompareResult",
    "RepositoryGitClient",
    "assert_inside_root",
    "is_link_or_junction",
    "is_sha",
    "remove_readonly",
]
