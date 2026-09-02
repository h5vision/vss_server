"""원격 Branch 조회와 선택 Branch object만 보존하는 Git cache client."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from backend.features.commit_catalog.errors import CommitCatalogError
from backend.features.commit_catalog.schemas import CommitGraphEntry, CommitGraphScanResult
from backend.features.repositories.schemas import validate_branch_ref
from backend.features.repository_collection.errors import CollectionError
from backend.features.repository_collection.schemas import RemoteBranchHead


def _remove_readonly(function, path: str, _error) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None:
        return bool(is_junction())
    if os.name != "nt":
        return False
    try:
        return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, FileNotFoundError, OSError):
        return False


@dataclass(frozen=True, slots=True)
class RepositoryGitClient:
    """Git credential과 stderr를 외부 계약에 노출하지 않는 동기식 Git 경계."""

    root: Path
    command_timeout_seconds: float = 60.0

    def list_remote_heads(self, remote_url: str) -> list[RemoteBranchHead]:
        result = self._run(
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
            if not self._is_sha(commit_sha) or branch_ref in seen_refs:
                raise self._invalid_remote_response()
            seen_refs.add(branch_ref)
            branches.append(
                RemoteBranchHead(
                    branch_ref=branch_ref,
                    commit_sha=commit_sha.lower(),
                )
            )
        return sorted(branches, key=lambda item: item.branch_ref)

    def fetch_branch(
        self,
        *,
        repository_id: UUID,
        tracked_branch_id: UUID,
        remote_url: str,
        branch_ref: str,
    ) -> str:
        validate_branch_ref(branch_ref)
        cache = self._ensure_cache(repository_id, remote_url)
        short_name = branch_ref.removeprefix("refs/heads/")
        cache_ref = f"refs/remotes/origin/{short_name}"
        self._run(
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
        commit_sha = self._output(
            ["git", "-C", str(cache), "rev-parse", f"{cache_ref}^{{commit}}"],
            failure=CollectionError(
                reason="REPOSITORY_REVISION_UNAVAILABLE",
                detail="선택한 Branch의 HEAD commit을 Git cache에서 확인하지 못했습니다.",
                retryable=True,
                status_code=503,
            ),
        ).lower()
        if not self._is_sha(commit_sha):
            raise self._invalid_remote_response()

        # remote ref가 force-push나 삭제로 이동해도 관측한 commit object가 GC되지 않도록
        # Backend 전용 보존 ref를 추가한다. API에는 이 내부 ref와 cache 경로를 노출하지 않는다.
        archive_ref = f"refs/vss-history/{tracked_branch_id.hex}/{commit_sha}"
        self._run(
            ["git", "-C", str(cache), "update-ref", archive_ref, commit_sha],
            failure=CollectionError(
                reason="REPOSITORY_CACHE_FAILED",
                detail="관측한 Branch HEAD를 Git cache에 보존하지 못했습니다.",
                retryable=True,
                status_code=500,
            ),
        )
        return commit_sha

    def is_ancestor(self, repository_id: UUID, previous_sha: str, observed_sha: str) -> bool:
        cache = self._cache_path(repository_id)
        if not cache.is_dir():
            raise CollectionError(
                reason="REPOSITORY_CACHE_UNAVAILABLE",
                detail="Branch 변경 방향을 확인할 Git cache가 없습니다.",
                retryable=True,
                status_code=503,
            )
        result = self._run(
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

    def checkout_revision(
        self,
        *,
        repository_id: UUID,
        revision: str,
        destination: Path,
    ) -> None:
        cache = self._cache_path(repository_id)
        if not cache.is_dir():
            raise CollectionError(
                reason="REPOSITORY_CACHE_UNAVAILABLE",
                detail="Snapshot 전체 tree를 만들 Git cache가 없습니다.",
                retryable=True,
                status_code=503,
            )
        self._run(
            ["git", "clone", "--quiet", "--no-checkout", "--", str(cache), str(destination)],
            failure=CollectionError(
                reason="SNAPSHOT_SOURCE_UNAVAILABLE",
                detail="관측한 commit의 전체 Git tree를 준비하지 못했습니다.",
                retryable=True,
                status_code=503,
            ),
        )
        self._run(
            ["git", "-C", str(destination), "checkout", "--quiet", "--detach", revision],
            failure=CollectionError(
                reason="REPOSITORY_REVISION_UNAVAILABLE",
                detail="관측한 commit을 Snapshot staging에 checkout하지 못했습니다.",
                retryable=True,
                status_code=503,
            ),
        )
        self.verify_checkout(destination, revision)

    def scan_commit_graph(
        self,
        *,
        repository_id: UUID,
        roots: list[str],
        max_commits: int,
        timeout_seconds: float,
        subject_max_length: int,
    ) -> CommitGraphScanResult:
        raw_roots = sorted({value.strip().lower() for value in roots})
        invalid_roots = [value for value in raw_roots if not self._is_sha(value)]
        if invalid_roots:
            raise CommitCatalogError(
                reason="COMMIT_CATALOG_ROOT_INVALID",
                detail="Commit catalog root에 유효하지 않은 Git SHA가 포함되어 있습니다.",
                retryable=False,
                status_code=409,
            )
        normalized_roots = raw_roots
        cache = self._cache_path(repository_id)
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
        object_format = self._output(
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
            result = self._run(
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

        shallow = self._output(
            ["git", "-C", str(cache), "rev-parse", "--is-shallow-repository"],
            failure=self._catalog_failure(),
        ) == "true"
        format_value = "%H%x00%T%x00%P%x00%an%x00%aI%x00%cI%x00%s"
        result = self._run(
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
            if not self._is_sha(commit_sha) or not self._is_sha(tree_sha) or any(
                not self._is_sha(parent) for parent in parent_shas
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

    def verify_checkout(self, project_root: Path, revision: str) -> None:
        head = self._output(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            failure=self._revision_mismatch(),
        ).lower()
        object_format = self._output(
            ["git", "-C", str(project_root), "rev-parse", "--show-object-format"],
            failure=self._revision_mismatch(),
        )
        status = self._output(
            [
                "git",
                "-C",
                str(project_root),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            failure=self._revision_mismatch(),
        )
        if head != revision.lower() or object_format != "sha1" or status:
            raise self._revision_mismatch()

    def _ensure_cache(self, repository_id: UUID, remote_url: str) -> Path:
        cache = self._cache_path(repository_id)
        cache.parent.mkdir(parents=True, exist_ok=True)
        self._assert_cache_path_safe(cache.parent)
        if cache.exists():
            self._assert_cache_path_safe(cache)
            is_bare = self._output(
                ["git", "-C", str(cache), "rev-parse", "--is-bare-repository"],
                failure=self._cache_failure(),
            )
            if is_bare != "true":
                raise self._cache_failure()
            self._run(
                ["git", "-C", str(cache), "remote", "set-url", "origin", remote_url],
                failure=self._cache_failure(),
            )
            return cache

        staging = self._inside_cache_root(cache.parent / f".{repository_id.hex}-{uuid4().hex}.tmp")
        try:
            self._run(
                ["git", "init", "--bare", "--quiet", str(staging)],
                failure=self._cache_failure(),
            )
            self._run(
                ["git", "-C", str(staging), "remote", "add", "origin", remote_url],
                failure=self._cache_failure(),
            )
            self._run(
                ["git", "-C", str(staging), "config", "gc.auto", "0"],
                failure=self._cache_failure(),
            )
            staging.replace(cache)
            self._assert_cache_path_safe(cache)
        finally:
            if staging.exists():
                shutil.rmtree(staging, onerror=_remove_readonly)
        return cache

    def _cache_path(self, repository_id: UUID) -> Path:
        return self._inside_cache_root(self._cache_root / f"{repository_id.hex}.git")

    @property
    def _cache_root(self) -> Path:
        resolved_root = self.root.expanduser().resolve()
        if resolved_root == Path(resolved_root.anchor):
            raise ValueError("repository cache root must not be a filesystem root")
        return resolved_root / ".repository-cache"

    def _inside_cache_root(self, path: Path) -> Path:
        root = self._cache_root
        candidate = path if path.is_absolute() else root / path
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise self._cache_failure() from exc
        return candidate

    def _assert_cache_path_safe(self, path: Path) -> None:
        root = self._cache_root
        candidate = self._inside_cache_root(path)
        current = candidate
        while True:
            if (current.exists() or current.is_symlink()) and _is_link_or_junction(current):
                raise self._cache_failure()
            if current == root:
                return
            if current.parent == current:
                raise self._cache_failure()
            current = current.parent

    def _output(self, command: list[str], *, failure: CollectionError) -> str:
        return self._run(command, failure=failure).stdout.strip()

    def _run(
        self,
        command: list[str],
        *,
        failure: CollectionError,
        allowed_returncodes: set[int] | None = None,
        timeout_seconds: float | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        environment["GIT_CONFIG_GLOBAL"] = os.devnull
        environment["GCM_INTERACTIVE"] = "Never"
        environment.pop("GIT_DIR", None)
        environment.pop("GIT_WORK_TREE", None)
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds or self.command_timeout_seconds,
                env=environment,
                input=input_text,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise failure from exc
        accepted = allowed_returncodes or {0}
        if result.returncode not in accepted:
            raise failure
        return result

    @staticmethod
    def _is_sha(value: str) -> bool:
        return len(value) == 40 and all(
            character in "0123456789abcdefABCDEF" for character in value
        )

    @staticmethod
    def _invalid_remote_response() -> CollectionError:
        return CollectionError(
            reason="REPOSITORY_REMOTE_INVALID_RESPONSE",
            detail="Repository가 유효한 SHA-1 Branch 목록을 반환하지 않았습니다.",
            retryable=False,
            status_code=502,
        )

    @staticmethod
    def _cache_failure() -> CollectionError:
        return CollectionError(
            reason="REPOSITORY_CACHE_FAILED",
            detail="Repository Git cache를 안전하게 준비하지 못했습니다.",
            retryable=True,
            status_code=500,
        )

    @staticmethod
    def _revision_mismatch() -> CollectionError:
        return CollectionError(
            reason="SNAPSHOT_REVISION_MISMATCH",
            detail="materialized Git HEAD 또는 working tree가 관측한 revision과 다릅니다.",
            retryable=False,
            status_code=409,
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
