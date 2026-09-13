"""Git adapter for discovering remote references without cloning."""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.features.repositories.schemas import validate_branch_ref
from backend.features.repository_collection.errors import CollectionError
from backend.features.repository_collection.schemas import RemoteBranchHead
from backend.infrastructure.git.runner import GitCommandRunner, is_sha
from backend.ports.git import RemoteRefReader


@dataclass(frozen=True, slots=True)
class GitRemoteRefAdapter(RemoteRefReader):
    """Adapter implementing RemoteRefReader using Git CLI ls-remote."""

    runner: GitCommandRunner = field(default_factory=GitCommandRunner)

    def list_remote_heads(self, remote_url: str) -> list[RemoteBranchHead]:
        result = self.runner.run(
            ["git", "ls-remote", "--heads", "--", remote_url],
            failure=CollectionError(
                reason="REPOSITORY_REMOTE_UNAVAILABLE",
                detail="Repository 원격 Branch 목록을 조회할 수 없습니다.",
                retryable=True,
                status_code=503,
            ),
        )
        branches: list[RemoteBranchHead] = []
        seen_refs: set[str] = set()
        for raw_line in result.stdout.splitlines():
            parts = raw_line.strip().split("\t", maxsplit=1)
            if len(parts) != 2:
                raise self._invalid_remote_response()
            commit_sha, branch_ref = parts
            try:
                validate_branch_ref(branch_ref)
            except ValueError as exc:
                raise self._invalid_remote_response() from exc
            if not is_sha(commit_sha) or branch_ref in seen_refs:
                raise self._invalid_remote_response()
            seen_refs.add(branch_ref)
            branches.append(
                RemoteBranchHead(
                    branch_ref=branch_ref,
                    commit_sha=commit_sha.lower(),
                )
            )
        return sorted(branches, key=lambda item: item.branch_ref)

    @staticmethod
    def _invalid_remote_response() -> CollectionError:
        return CollectionError(
            reason="REPOSITORY_REMOTE_INVALID_RESPONSE",
            detail="Repository가 유효한 SHA-1 Branch/Tag 목록을 반환하지 않았습니다.",
            retryable=False,
            status_code=502,
        )
