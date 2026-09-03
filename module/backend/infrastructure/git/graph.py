"""Git adapter for inspecting commit ancestry and scanning topological graphs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from backend.features.commit_catalog.errors import CommitCatalogError
from backend.features.commit_catalog.schemas import CommitGraphEntry, CommitGraphScanResult
from backend.features.repository_collection.errors import CollectionError
from backend.infrastructure.git.layout import GitCacheLayout
from backend.infrastructure.git.runner import GitCommandRunner, is_sha
from backend.ports.git import CommitGraphReader


@dataclass(frozen=True, slots=True)
class GitCommitGraphAdapter(CommitGraphReader):
    """Adapter implementing CommitGraphReader using Git CLI rev-list and merge-base."""

    layout: GitCacheLayout
    runner: GitCommandRunner = field(default_factory=GitCommandRunner)

    def is_ancestor(self, repository_id: UUID, previous_sha: str, observed_sha: str) -> bool:
        cache = self.layout.cache_path(repository_id)
        if not cache.is_dir():
            raise CollectionError(
                reason="REPOSITORY_CACHE_UNAVAILABLE",
                detail="Branch 변경 방향을 확인할 Git cache가 없습니다.",
                retryable=True,
                status_code=503,
            )
        result = self.runner.run(
            [
                "git",
                "-C",
                str(cache),
                "merge-base",
                "--is-ancestor",
                previous_sha,
                observed_sha,
            ],
            failure=CollectionError(
                reason="REPOSITORY_HISTORY_UNAVAILABLE",
                detail="이전 HEAD와 새 HEAD의 Git 관계를 확인하지 못했습니다.",
                retryable=True,
                status_code=503,
            ),
            allowed_returncodes={0, 1},
        )
        return result.returncode == 0

    def has_commit(self, repository_id: UUID, commit_sha: str) -> bool:
        cache = self.layout.cache_path(repository_id)
        if not cache.is_dir():
            return False
        result = self.runner.run(
            ["git", "-C", str(cache), "cat-file", "-e", f"{commit_sha}^{{commit}}"],
            failure=CollectionError(
                reason="REPOSITORY_REVISION_UNAVAILABLE",
                detail="커밋 object 존재를 확인하지 못했습니다.",
                retryable=True,
                status_code=503,
            ),
            allowed_returncodes={0, 1},
        )
        return result.returncode == 0

    def scan_commit_graph(
        self,
        *,
        repository_id: UUID,
        roots: list[str],
        max_commits: int = 500,
        timeout_seconds: float = 60.0,
        subject_max_length: int = 255,
    ) -> CommitGraphScanResult:
        raw_roots = sorted({value.strip().lower() for value in roots})
        invalid_roots = [value for value in raw_roots if not is_sha(value)]
        if invalid_roots:
            raise CommitCatalogError(
                reason="COMMIT_CATALOG_ROOT_INVALID",
                detail="Commit catalog root에 유효하지 않은 Git SHA가 포함되어 있습니다.",
                retryable=False,
                status_code=409,
            )
        normalized_roots = raw_roots
        cache = self.layout.cache_path(repository_id)
        if not cache.is_dir():
            raise CommitCatalogError(
                reason="COMMIT_CATALOG_CACHE_UNAVAILABLE",
                detail="Commit graph를 읽을 Repository Git cache가 없습니다.",
                retryable=True,
                status_code=503,
            )
        if not normalized_roots:
            raise CommitCatalogError(
                reason="COMMIT_CATALOG_ROOTS_REQUIRED",
                detail="Commit catalog를 만들 검증된 revision root가 없습니다.",
                retryable=False,
                status_code=409,
            )
        object_format = self.runner.output(
            ["git", "-C", str(cache), "rev-parse", "--show-object-format"],
            failure=self._catalog_failure(),
        )
        if object_format != "sha1":
            raise CommitCatalogError(
                reason="COMMIT_CATALOG_OBJECT_FORMAT_UNSUPPORTED",
                detail="현재 Commit Catalog는 SHA-1 Git Repository만 지원합니다.",
                retryable=False,
                status_code=409,
            )

        available_roots: list[str] = []
        unavailable_roots: list[str] = []
        for revision in normalized_roots:
            result = self.runner.run(
                ["git", "-C", str(cache), "cat-file", "-e", f"{revision}^{{commit}}"],
                failure=self._catalog_failure(),
                allowed_returncodes={0, 1, 128},
                timeout_seconds=timeout_seconds,
            )
            target = available_roots if result.returncode == 0 else unavailable_roots
            target.append(revision)
        if not available_roots:
            raise CommitCatalogError(
                reason="COMMIT_CATALOG_ROOTS_UNAVAILABLE",
                detail="요청한 revision root의 commit object를 Git cache에서 찾지 못했습니다.",
                retryable=True,
                status_code=503,
            )

        shallow = (
            self.runner.output(
                ["git", "-C", str(cache), "rev-parse", "--is-shallow-repository"],
                failure=self._catalog_failure(),
            )
            == "true"
        )
        format_value = "%H%x00%T%x00%P%x00%an%x00%aI%x00%cI%x00%s"
        result = self.runner.run(
            [
                "git",
                "-C",
                str(cache),
                "rev-list",
                "--topo-order",
                f"--max-count={max_commits + 1}",
                f"--format=format:{format_value}",
                "--stdin",
            ],
            failure=self._catalog_failure(),
            timeout_seconds=timeout_seconds,
            input_text="\n".join(available_roots) + "\n",
        )
        entries: list[CommitGraphEntry] = []
        for raw_line in result.stdout.splitlines():
            if raw_line.startswith("commit "):
                continue
            fields = raw_line.split("\x00", maxsplit=6)
            if len(fields) != 7:
                raise self._catalog_invalid_output()
            commit_sha, tree_sha, parents, author, authored_at, committed_at, subject = fields
            parent_shas = parents.split() if parents else []
            if not is_sha(commit_sha) or not is_sha(tree_sha) or any(
                not is_sha(parent) for parent in parent_shas
            ):
                raise self._catalog_invalid_output()
            try:
                parsed_authored_at = datetime.fromisoformat(authored_at)
                parsed_committed_at = datetime.fromisoformat(committed_at)
            except ValueError as exc:
                raise self._catalog_invalid_output() from exc
            entries.append(
                CommitGraphEntry(
                    commit_sha=commit_sha.lower(),
                    tree_sha=tree_sha.lower(),
                    parent_shas=[value.lower() for value in parent_shas],
                    author_name=self._clean_metadata(author, 255) or None,
                    authored_at=parsed_authored_at,
                    committed_at=parsed_committed_at,
                    subject=self._clean_metadata(subject, subject_max_length),
                )
            )
        truncated = len(entries) > max_commits
        entries = entries[:max_commits]
        return CommitGraphScanResult(
            roots=available_roots,
            unavailable_roots=unavailable_roots,
            entries=entries,
            truncated=truncated,
            shallow=shallow,
            history_complete=not truncated and not shallow and not unavailable_roots,
        )

    @staticmethod
    def _clean_metadata(value: str, max_length: int) -> str:
        safe = "".join(
            character if ord(character) >= 32 and ord(character) != 127 else " "
            for character in value
        )
        return " ".join(safe.split())[:max_length]

    @staticmethod
    def _catalog_failure() -> CommitCatalogError:
        return CommitCatalogError(
            reason="COMMIT_CATALOG_GIT_FAILED",
            detail="Repository commit graph를 Git cache에서 읽지 못했습니다.",
            retryable=True,
            status_code=503,
        )

    @staticmethod
    def _catalog_invalid_output() -> CommitCatalogError:
        return CommitCatalogError(
            reason="COMMIT_CATALOG_GIT_INVALID_RESPONSE",
            detail="Git cache가 유효한 commit graph metadata를 반환하지 않았습니다.",
            retryable=False,
            status_code=500,
        )
