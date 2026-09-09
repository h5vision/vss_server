"""Git adapter for fetching remote branches, tags, and change request objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from backend.features.repositories.schemas import validate_branch_ref
from backend.features.repository_collection.errors import CollectionError
from backend.infrastructure.git.layout import GitCacheLayout
from backend.infrastructure.git.runner import GitCommandRunner, is_sha
from backend.ports.git import RemoteObjectFetcher


@dataclass(frozen=True, slots=True)
class GitRemoteObjectAdapter(RemoteObjectFetcher):
    """Adapter implementing RemoteObjectFetcher by fetching into bare Git caches."""

    layout: GitCacheLayout
    runner: GitCommandRunner = field(default_factory=GitCommandRunner)

    def fetch_branch(
        self,
        *,
        repository_id: UUID,
        remote_url: str,
        branch_ref: str,
        tracked_branch_id: UUID | None = None,
        expected_commit_sha: str | None = None,
    ) -> str:
        validate_branch_ref(branch_ref)
        cache = self.layout.ensure_cache(repository_id, self.runner, remote_url=remote_url)
        short_name = branch_ref.removeprefix("refs/heads/")
        cache_ref = f"refs/remotes/origin/{short_name}"
        self.runner.run(
            [
                "git",
                "-C",
                str(cache),
                "fetch",
                "--quiet",
                "--force",
                "--no-tags",
                "--no-recurse-submodules",
                "origin",
                f"{branch_ref}:{cache_ref}",
            ],
            failure=CollectionError(
                reason="REPOSITORY_FETCH_FAILED",
                detail="선택한 Branch의 Git object를 가져오지 못했습니다.",
                retryable=True,
                status_code=503,
            ),
        )
        commit_sha = self.runner.output(
            ["git", "-C", str(cache), "rev-parse", f"{cache_ref}^{{commit}}"],
            failure=CollectionError(
                reason="REPOSITORY_REVISION_UNAVAILABLE",
                detail="선택한 Branch의 HEAD commit을 Git cache에서 확인하지 못했습니다.",
                retryable=True,
                status_code=503,
            ),
        ).lower()
        if not is_sha(commit_sha):
            raise CollectionError(
                reason="REPOSITORY_REMOTE_INVALID_RESPONSE",
                detail="Repository가 유효한 SHA-1 Branch 목록을 반환하지 않았습니다.",
                retryable=False,
                status_code=502,
            )

        if expected_commit_sha and commit_sha != expected_commit_sha.lower():
            raise CollectionError(
                reason="REPOSITORY_REVISION_MISMATCH",
                detail="원격 Branch와 관측한 commit SHA가 일치하지 않습니다.",
                retryable=True,
                status_code=409,
            )

        # remote ref가 force-push나 삭제로 이동해도 관측한 commit object가 GC되지 않도록
        # 내부 보존 ref를 추가한다.
        if tracked_branch_id:
            archive_ref = f"refs/vss-history/{tracked_branch_id.hex}/{commit_sha}"
            self.runner.run(
                ["git", "-C", str(cache), "update-ref", archive_ref, commit_sha],
                failure=CollectionError(
                    reason="REPOSITORY_CACHE_FAILED",
                    detail="관측한 Branch HEAD를 Git cache에 보존하지 못했습니다.",
                    retryable=True,
                    status_code=500,
                ),
            )
        return commit_sha
