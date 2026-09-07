"""Branch-scoped mutable Repository working copies under SNAPSHOT_REPOSITORY_ROOT."""

from __future__ import annotations

import hashlib
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
    """Creates one stable mutable working copy for each registered Repository Branch."""

    root: Path
    runner: GitCommandRunner = field(default_factory=GitCommandRunner)

    @property
    def repository_root(self) -> Path:
        resolved = self.root.expanduser().resolve()
        if resolved == Path(resolved.anchor):
            raise ValueError("repository workspace root must not be a filesystem root")
        return resolved

    def workspace_path(
        self,
        *,
        repository_id: UUID,
        canonical_name: str,
        branch_ref: str,
    ) -> Path:
        del repository_id  # identity is verified through local Git config, not encoded in the path.
        repository_name = self._safe_repository_basename(canonical_name)
        branch_name = self._safe_branch_component(self._branch_name(branch_ref))
        candidate = self.repository_root / f"{repository_name}--{branch_name}"
        try:
            assert_inside_root(candidate, self.repository_root)
        except ValueError as exc:
            raise self._unsafe_workspace() from exc
        return candidate

    def ensure_branch(
        self,
        *,
        repository_id: UUID,
        canonical_name: str,
        remote_url: str,
        branch_ref: str,
        expected_revision: str | None = None,
        refresh_existing: bool = True,
    ) -> Path:
        branch = self._branch_name(branch_ref)
        root = self.repository_root
        root.mkdir(parents=True, exist_ok=True)
        if is_link_or_junction(root):
            raise self._unsafe_workspace()

        workspace = self.workspace_path(
            repository_id=repository_id,
            canonical_name=canonical_name,
            branch_ref=branch_ref,
        )
        existed = workspace.exists() or workspace.is_symlink()
        if existed:
            resolved = self._refresh_existing(
                workspace,
                repository_id=repository_id,
                remote_url=remote_url,
                branch_ref=branch_ref,
                branch=branch,
                expected_revision=expected_revision,
                refresh=refresh_existing,
            )
        else:
            resolved = self._clone_new(
                workspace,
                repository_id=repository_id,
                remote_url=remote_url,
                branch_ref=branch_ref,
                branch=branch,
                expected_revision=expected_revision,
            )
        if expected_revision is not None and (refresh_existing or not existed):
            self._assert_expected_revision(resolved, expected_revision)
        return resolved

    def ensure_repository(
        self,
        *,
        repository_id: UUID,
        canonical_name: str,
        remote_url: str,
        default_branch_ref: str,
    ) -> Path:
        """Compatibility wrapper: the default Branch is still a branch-scoped workspace."""
        return self.ensure_branch(
            repository_id=repository_id,
            canonical_name=canonical_name,
            remote_url=remote_url,
            branch_ref=default_branch_ref,
        )

    def _clone_new(
        self,
        workspace: Path,
        *,
        repository_id: UUID,
        remote_url: str,
        branch_ref: str,
        branch: str,
        expected_revision: str | None,
    ) -> Path:
        staging = self.repository_root / f".{workspace.name}-{uuid4().hex}.tmp"
        try:
            assert_inside_root(staging, self.repository_root)
            self.runner.run(
                ["git", "clone", "--quiet", "--no-checkout", "--", remote_url, str(staging)],
                failure=self._workspace_failure(),
            )
            self._write_identity(
                staging,
                repository_id=repository_id,
                branch_ref=branch_ref,
            )
            if expected_revision is None:
                self._checkout_remote_branch(staging, branch=branch)
            else:
                self._assert_remote_branch_revision(staging, branch, expected_revision)
                self._checkout_revision(staging, expected_revision)
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

    def _refresh_existing(
        self,
        workspace: Path,
        *,
        repository_id: UUID,
        remote_url: str,
        branch_ref: str,
        branch: str,
        expected_revision: str | None,
        refresh: bool,
    ) -> Path:
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
        self._assert_workspace_identity(
            workspace,
            repository_id=repository_id,
            remote_url=remote_url,
            branch_ref=branch_ref,
        )
        if not refresh:
            return workspace.resolve()
        self.runner.run(
            ["git", "-C", str(workspace), "remote", "set-url", "origin", remote_url],
            failure=self._workspace_failure(),
        )
        self.runner.run(
            ["git", "-C", str(workspace), "fetch", "--quiet", "--prune", "--tags", "origin"],
            failure=self._workspace_failure(),
        )
        if expected_revision is None:
            self._checkout_remote_branch(workspace, branch=branch)
        else:
            # Prove remote HEAD before changing the VSS-visible working copy.
            self._assert_remote_branch_revision(workspace, branch, expected_revision)
            self._checkout_revision(workspace, expected_revision)
        self._assert_clean_worktree(workspace)
        return workspace.resolve()

    def _assert_workspace_identity(
        self,
        workspace: Path,
        *,
        repository_id: UUID,
        remote_url: str,
        branch_ref: str,
    ) -> None:
        configured_repository = self._optional_config(workspace, "sol.repository-id")
        configured_branch = self._optional_config(workspace, "sol.branch-ref")
        configured_remote = self.runner.output(
            ["git", "-C", str(workspace), "remote", "get-url", "origin"],
            failure=self._workspace_failure(),
        )
        if configured_repository not in {None, str(repository_id)}:
            raise self._workspace_collision()
        if configured_branch not in {None, branch_ref}:
            raise self._workspace_collision()
        if configured_repository is None and configured_remote != remote_url:
            raise self._workspace_collision()
        if configured_repository is None or configured_branch is None:
            self._write_identity(
                workspace,
                repository_id=repository_id,
                branch_ref=branch_ref,
            )

    def _write_identity(self, workspace: Path, *, repository_id: UUID, branch_ref: str) -> None:
        self.runner.run(
            ["git", "-C", str(workspace), "config", "sol.repository-id", str(repository_id)],
            failure=self._workspace_failure(),
        )
        self.runner.run(
            ["git", "-C", str(workspace), "config", "sol.branch-ref", branch_ref],
            failure=self._workspace_failure(),
        )

    def _optional_config(self, workspace: Path, key: str) -> str | None:
        result = self.runner.run(
            ["git", "-C", str(workspace), "config", "--get", key],
            allowed_returncodes={0, 1},
            failure=self._workspace_failure(),
        )
        if result.returncode == 1:
            return None
        return result.stdout.strip() or None

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

    def _checkout_revision(self, workspace: Path, revision: str) -> None:
        self.runner.run(
            ["git", "-C", str(workspace), "checkout", "--quiet", "--detach", revision],
            failure=self._workspace_failure(),
        )

    def _assert_remote_branch_revision(
        self,
        workspace: Path,
        branch: str,
        expected_revision: str,
    ) -> None:
        actual = self.runner.output(
            ["git", "-C", str(workspace), "rev-parse", f"refs/remotes/origin/{branch}"],
            failure=self._workspace_failure(),
        )
        if actual != expected_revision:
            raise self._branch_head_mismatch()

    def _assert_expected_revision(self, workspace: Path, expected_revision: str | None) -> None:
        if expected_revision is None:
            return
        actual = self.runner.output(
            ["git", "-C", str(workspace), "rev-parse", "HEAD"],
            failure=self._workspace_failure(),
        )
        if actual != expected_revision:
            raise self._branch_head_mismatch()

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

    def _branch_name(self, branch_ref: str) -> str:
        prefix = "refs/heads/"
        if not branch_ref.startswith(prefix):
            raise self._invalid_branch()
        branch = branch_ref[len(prefix) :].strip()
        if not branch:
            raise self._invalid_branch()
        self.runner.run(
            ["git", "check-ref-format", "--branch", branch],
            failure=self._invalid_branch(),
        )
        return branch

    @staticmethod
    def _safe_repository_basename(canonical_name: str) -> str:
        normalized = canonical_name.strip().replace("\\", "/").rstrip("/")
        raw_name = normalized.rsplit("/", 1)[-1] if normalized else "repository"
        if raw_name.endswith(".git"):
            raw_name = raw_name[:-4]
        safe = _SAFE_COMPONENT.sub("-", raw_name).strip("._-") or "repository"
        return safe[:96].rstrip("._-") or "repository"

    @staticmethod
    def _safe_branch_component(branch: str) -> str:
        parts: list[str] = []
        collision_risk = False
        for raw_part in branch.replace("\\", "/").split("/"):
            part = _SAFE_COMPONENT.sub("-", raw_part).strip("._-")
            if part != raw_part or "--" in raw_part:
                collision_risk = True
            if part:
                parts.append(part)
        safe = "--".join(parts) or "branch"
        if len(safe) > 128:
            collision_risk = True
            safe = safe[:118].rstrip("._-") or "branch"
        if collision_risk:
            digest = hashlib.sha256(branch.encode("utf-8")).hexdigest()[:8]
            base = safe[:118].rstrip("._-") or "branch"
            safe = f"{base}--{digest}"
        return safe

    @staticmethod
    def _branch_head_mismatch() -> CollectionError:
        return CollectionError(
            reason="REPOSITORY_BRANCH_HEAD_MISMATCH",
            detail=(
                "관리 Branch의 원격 HEAD가 요청한 revision과 다릅니다. "
                "Repository를 다시 Sync한 뒤 인덱싱해야 합니다."
            ),
            retryable=True,
            status_code=409,
        )

    @staticmethod
    def _workspace_failure() -> CollectionError:
        return CollectionError(
            reason="REPOSITORY_WORKSPACE_FAILED",
            detail="관리 Repository Branch working copy를 준비하거나 갱신하지 못했습니다.",
            retryable=True,
            status_code=503,
        )

    @staticmethod
    def _workspace_collision() -> CollectionError:
        return CollectionError(
            reason="REPOSITORY_WORKSPACE_COLLISION",
            detail=(
                "같은 Repository 기본 이름과 Branch 이름의 관리 경로가 다른 저장소와 충돌합니다."
            ),
            retryable=False,
            status_code=409,
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
    def _invalid_branch() -> CollectionError:
        return CollectionError(
            reason="REPOSITORY_BRANCH_INVALID",
            detail="Repository Branch ref가 유효한 refs/heads/* 형식이 아닙니다.",
            retryable=False,
            status_code=422,
        )
