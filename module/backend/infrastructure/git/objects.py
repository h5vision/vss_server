"""Git adapter for fetching remote branches, tags, and change request objects."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from uuid import UUID

from backend.features.repositories.schemas import validate_branch_ref, validate_tag_ref
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

    def fetch_tag(
        self,
        *,
        repository_id: UUID,
        remote_url: str,
        tag_ref: str,
        expected_commit_sha: str,
    ) -> None:
        validate_tag_ref(tag_ref)
        if not is_sha(expected_commit_sha):
            raise CollectionError(
                reason="TAG_REVISION_INVALID",
                detail="Tag에 유효하지 않은 Git revision이 포함되어 있습니다.",
                retryable=False,
                status_code=409,
            )
        cache = self.layout.ensure_cache(repository_id, self.runner, remote_url=remote_url)
        tag_key = hashlib.sha256(tag_ref.encode("utf-8")).hexdigest()[:24]
        prefix = f"refs/vss-tags/{tag_key}"
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
                f"+{tag_ref}:{prefix}/source",
            ],
            failure=CollectionError(
                reason="TAG_FETCH_FAILED",
                detail="선택한 Tag의 Git object를 가져오지 못했습니다.",
                retryable=True,
                status_code=503,
            ),
        )
        resolved = self.runner.output(
            ["git", "-C", str(cache), "rev-parse", f"{prefix}/source^{{commit}}"],
            failure=CollectionError(
                reason="TAG_REVISION_UNAVAILABLE",
                detail="Tag commit을 Git cache에서 확인하지 못했습니다.",
                retryable=True,
                status_code=503,
            ),
        ).lower()
        if resolved != expected_commit_sha.lower():
            raise CollectionError(
                reason="TAG_REVISION_MISMATCH",
                detail="원격 Tag와 관측한 commit SHA가 일치하지 않습니다.",
                retryable=True,
                status_code=409,
            )
        self.runner.run(
            [
                "git",
                "-C",
                str(cache),
                "update-ref",
                f"{prefix}/revisions/{resolved}",
                resolved,
            ],
            failure=CollectionError(
                reason="REPOSITORY_CACHE_FAILED",
                detail="검증한 Tag commit을 Git cache에 보존하지 못했습니다.",
                retryable=True,
                status_code=500,
            ),
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
        validate_branch_ref(base_ref)
        if provider not in {"github", "gitlab"} or external_number <= 0:
            raise CollectionError(
                reason="CHANGE_REQUEST_REF_INVALID",
                detail="지원하지 않는 provider 또는 Change Request 번호입니다.",
                retryable=False,
                status_code=422,
            )
        revisions = [base_sha, head_sha, *([merge_sha] if merge_sha else [])]
        if any(not is_sha(revision) for revision in revisions):
            raise CollectionError(
                reason="CHANGE_REQUEST_REVISION_INVALID",
                detail="Change Request에 유효하지 않은 Git revision이 포함되어 있습니다.",
                retryable=False,
                status_code=409,
            )
        provider_ref = (
            f"refs/pull/{external_number}/head"
            if provider == "github"
            else f"refs/merge-requests/{external_number}/head"
        )
        prefix = f"refs/vss-change-requests/{provider}/{external_number}"
        cache = self.layout.ensure_cache(repository_id, self.runner, remote_url=remote_url)
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
                f"+{base_ref}:{prefix}/base",
                f"+{provider_ref}:{prefix}/head",
            ],
            failure=CollectionError(
                reason="CHANGE_REQUEST_FETCH_FAILED",
                detail="PR/MR의 base와 provider-owned head ref를 가져오지 못했습니다.",
                retryable=True,
                status_code=503,
            ),
        )
        fetched_head = self.runner.output(
            ["git", "-C", str(cache), "rev-parse", f"{prefix}/head^{{commit}}"],
            failure=CollectionError(
                reason="CHANGE_REQUEST_REVISION_UNAVAILABLE",
                detail="PR/MR head revision을 Git cache에서 확인하지 못했습니다.",
                retryable=True,
                status_code=503,
            ),
        ).lower()
        if fetched_head != head_sha.lower():
            raise CollectionError(
                reason="CHANGE_REQUEST_REVISION_MISMATCH",
                detail="Provider API head SHA와 provider-owned Git ref가 일치하지 않습니다.",
                retryable=True,
                status_code=409,
            )
        for revision in revisions:
            resolved = self.runner.output(
                ["git", "-C", str(cache), "rev-parse", f"{revision}^{{commit}}"],
                failure=CollectionError(
                    reason="CHANGE_REQUEST_REVISION_UNAVAILABLE",
                    detail="PR/MR revision을 Git cache에서 확인하지 못했습니다.",
                    retryable=True,
                    status_code=503,
                ),
            ).lower()
            if resolved != revision.lower():
                raise CollectionError(
                    reason="CHANGE_REQUEST_REVISION_MISMATCH",
                    detail="Provider API revision과 Git commit object가 일치하지 않습니다.",
                    retryable=False,
                    status_code=409,
                )
            self.runner.run(
                [
                    "git",
                    "-C",
                    str(cache),
                    "update-ref",
                    f"{prefix}/revisions/{revision.lower()}",
                    revision.lower(),
                ],
                failure=CollectionError(
                    reason="REPOSITORY_CACHE_FAILED",
                    detail="검증한 PR/MR revision을 Git cache에 보존하지 못했습니다.",
                    retryable=True,
                    status_code=500,
                ),
            )
