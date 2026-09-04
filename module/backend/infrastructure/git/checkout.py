"""Git adapter for materializing exact commit trees to disk and verifying integrity."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from backend.features.repository_collection.errors import CollectionError
from backend.infrastructure.git.layout import GitCacheLayout
from backend.infrastructure.git.runner import GitCommandRunner
from backend.ports.git import RevisionTreeMaterializer


@dataclass(frozen=True, slots=True)
class GitTreeCheckoutAdapter(RevisionTreeMaterializer):
    """Adapter implementing RevisionTreeMaterializer using Git clone and checkout."""

    layout: GitCacheLayout
    runner: GitCommandRunner = field(default_factory=GitCommandRunner)

    def checkout_revision(
        self,
        *,
        repository_id: UUID,
        revision: str,
        destination: Path,
    ) -> Path:
        cache = self.layout.cache_path(repository_id)
        if not cache.is_dir():
            raise CollectionError(
                reason="REPOSITORY_CACHE_UNAVAILABLE",
                detail="Snapshot 전체 tree를 만들 Git cache가 없습니다.",
                retryable=True,
                status_code=503,
            )
        self.runner.run(
            ["git", "clone", "--quiet", "--no-checkout", "--", str(cache), str(destination)],
            failure=CollectionError(
                reason="SNAPSHOT_SOURCE_UNAVAILABLE",
                detail="관측한 commit의 전체 Git tree를 준비하지 못했습니다.",
                retryable=True,
                status_code=503,
            ),
        )
        self.runner.run(
            ["git", "-C", str(destination), "checkout", "--quiet", "--detach", revision],
            failure=CollectionError(
                reason="REPOSITORY_REVISION_UNAVAILABLE",
                detail="관측한 commit을 Snapshot staging에 checkout하지 못했습니다.",
                retryable=True,
                status_code=503,
            ),
        )
        self.verify_checkout(destination, revision)
        return destination

    def verify_checkout(self, destination: Path, expected_tree_sha: str) -> None:
        head = self.runner.output(
            ["git", "-C", str(destination), "rev-parse", "HEAD"],
            failure=self._revision_mismatch(),
        ).lower()
        object_format = self.runner.output(
            ["git", "-C", str(destination), "rev-parse", "--show-object-format"],
            failure=self._revision_mismatch(),
        )
        status = self.runner.output(
            [
                "git",
                "-C",
                str(destination),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            failure=self._revision_mismatch(),
        )
        if head != expected_tree_sha.lower() or object_format != "sha1" or status:
            raise self._revision_mismatch()

    @staticmethod
    def _revision_mismatch() -> CollectionError:
        return CollectionError(
            reason="SNAPSHOT_REVISION_MISMATCH",
            detail="materialized Git HEAD 또는 working tree가 관측한 revision과 다릅니다.",
            retryable=False,
            status_code=409,
        )
