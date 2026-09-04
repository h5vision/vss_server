"""Mutable managed Repository working-copy adapter under SNAPSHOT_REPOSITORY_ROOT."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

from backend.features.repository_collection.errors import CollectionError
from backend.infrastructure.git.runner import (
    GitCommandRunner,
    assert_inside_root,
    is_link_or_junction,
    remove_readonly,
)
from backend.ports.git import ManagedRepositoryWorkspace

_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class RepositoryWorkspaceManager(ManagedRepositoryWorkspace):
    """Creates and refreshes collision-safe mutable working copies for registered Repositories."""

    root: Path
    runner: GitCommandRunner = field(default_factory=GitCommandRunner)

    @property
    def repository_root(self) -> Path:
        resolved = self.root.expanduser().resolve()
        if resolved == Path(resolved.anchor):
            raise ValueError("repository workspace root must not be a filesystem root")
        return resolved

    def workspace_path(self, *, repository_id: UUID, canonical_name: str) -> Path:
        safe_name = self._safe_repository_name(canonical_name)
        candidate = self.repository_root / f"{safe_name}--{repository_id.hex[:8]}"
        try:
            assert_inside_root(candidate, self.repository_root)
        except ValueError as exc:
            raise self._unsafe_workspace() from exc
        return candidate

    def ensure_repository(
        self,
        *,
        repository_id: UUID,
        canonical_name: str,
        remote_url: str,
        default_branch_ref: str,
    ) -> Path:
        branch = self._branch_name(default_branch_ref)
        root = self.repository_root
        root.mkdir(parents=True, exist_ok=True)
        if is_link_or_junction(root):
            raise self._unsafe_workspace()

        workspace = self.workspace_path(
            repository_id=repository_id,
            canonical_name=canonical_name,
        )
        if workspace.exists() or workspace.is_symlink():
            return self._refresh_existing(workspace, remote_url=remote_url, branch=branch)
        return self._clone_new(workspace, remote_url=remote_url, branch=branch)

    def _clone_new(self, workspace: Path, *, remote_url: str, branch: str) -> Path:
        staging = self.repository_root / f".{workspace.name}-{uuid4().hex}.tmp"
        try:
            assert_inside_root(staging, self.repository_root)
            self.runner.run(
                ["git", "clone", "--quiet", "--no-checkout", "--", remote_url, str(staging)],
                failure=self._workspace_failure(),
            )
            self._checkout_remote_branch(staging, branch=branch)
            self._assert_clean_worktree(staging)
            staging.replace(workspace)
            return workspace.resolve()
        except CollectionError:
            raise
        except (OSError, ValueError) as exc:
            raise self._workspace_failure() from exc
        finally:
            if staging.is_symlink():
                staging.unlink(missing_ok=True)
            elif staging.exists():
                shutil.rmtree(staging, onerror=remove_readonly)

    def _refresh_existing(self, workspace: Path, *, remote_url: str, branch: str) -> Path:
        try:
            assert_inside_root(workspace, self.repository_root)
        except ValueError as exc:
            raise self._unsafe_workspace() from exc
        if is_link_or_junction(workspace) or not workspace.is_dir():
            raise self._unsafe_workspace()

        is_worktree = self.runner.output(
            ["git", "-C", str(workspace), "rev-parse", "--is-inside-work-tree"],
            failure=self._workspace_failure(),
        )
        if is_worktree != "true":
            raise self._workspace_failure()
        self._assert_clean_worktree(workspace)
        self.runner.run(
            ["git", "-C", str(workspace), "remote", "set-url", "origin", remote_url],
            failure=self._workspace_failure(),
        )
        self.runner.run(
            ["git", "-C", str(workspace), "fetch", "--quiet", "--prune", "--tags", "origin"],
            failure=self._workspace_failure(),
        )
        self._checkout_remote_branch(workspace, branch=branch)
        self._assert_clean_worktree(workspace)
        return workspace.resolve()

    def _checkout_remote_branch(self, workspace: Path, *, branch: str) -> None:
        self.runner.run(
            [
                "git",
                "-C",
                str(workspace),
                "checkout",
                "--quiet",
                "--detach",
                f"refs/remotes/origin/{branch}",
            ],
            failure=self._workspace_failure(),
        )

    def _assert_clean_worktree(self, workspace: Path) -> None:
        status = self.runner.output(
            [
                "git",
                "-C",
                str(workspace),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            failure=self._workspace_failure(),
        )
        if status:
            raise CollectionError(
                reason="REPOSITORY_WORKSPACE_DIRTY",
                detail=(
                    "관리 Repository working copy에 로컬 변경이 있어 자동 갱신하지 않습니다."
                ),
                retryable=False,
                status_code=409,
            )

    def _branch_name(self, default_branch_ref: str) -> str:
        prefix = "refs/heads/"
        if not default_branch_ref.startswith(prefix):
            raise self._invalid_default_branch()
        branch = default_branch_ref[len(prefix) :].strip()
        if not branch:
            raise self._invalid_default_branch()
        self.runner.run(
            ["git", "check-ref-format", "--branch", branch],
            failure=self._invalid_default_branch(),
        )
        return branch

    @staticmethod
    def _safe_repository_name(canonical_name: str) -> str:
        normalized = canonical_name.strip().replace("\\", "/")
        parts: list[str] = []
        for raw_part in normalized.split("/"):
            if raw_part in {"", ".", ".."}:
                continue
            part = _SAFE_COMPONENT.sub("-", raw_part).strip("._-")
            if part:
                parts.append(part)
        safe = "--".join(parts) or "repository"
        return safe[:96].rstrip("._-") or "repository"

    @staticmethod
    def _workspace_failure() -> CollectionError:
        return CollectionError(
            reason="REPOSITORY_WORKSPACE_FAILED",
            detail="관리 Repository working copy를 준비하거나 갱신하지 못했습니다.",
            retryable=True,
            status_code=503,
        )

    @staticmethod
    def _unsafe_workspace() -> CollectionError:
        return CollectionError(
            reason="REPOSITORY_WORKSPACE_UNSAFE",
            detail="관리 Repository 경로가 허용된 repository root 경계를 벗어났습니다.",
            retryable=False,
            status_code=500,
        )

    @staticmethod
    def _invalid_default_branch() -> CollectionError:
        return CollectionError(
            reason="REPOSITORY_DEFAULT_BRANCH_INVALID",
            detail="Repository default Branch ref가 유효한 refs/heads/* 형식이 아닙니다.",
            retryable=False,
            status_code=422,
        )
