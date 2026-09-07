"""Tests for mutable managed Repository workspaces under SNAPSHOT_REPOSITORY_ROOT."""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from backend.features.repository_collection.errors import CollectionError
from backend.infrastructure.git.runner import GitCommandRunner
from backend.infrastructure.git.workspace import RepositoryWorkspaceManager


def _git(repository: Path, *arguments: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    return result.stdout.strip()


def _create_remote(root: Path) -> tuple[Path, Path, str]:
    remote = root / "remote.git"
    work = root / "source"
    remote.mkdir()
    work.mkdir()
    _git(remote, "init", "--bare")
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.name", "Workspace Test")
    _git(work, "config", "user.email", "workspace@example.invalid")
    _git(work, "remote", "add", "origin", str(remote))
    (work / "app.py").write_text("version = 1\n", encoding="utf-8")
    _git(work, "add", "--all")
    _git(work, "commit", "-m", "first")
    first_sha = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "-u", "origin", "main")
    return remote, work, first_sha


def test_workspace_manager_clones_and_fast_forwards_managed_repository(tmp_path: Path) -> None:
    remote, source, first_sha = _create_remote(tmp_path)
    repository_id = uuid4()
    manager = RepositoryWorkspaceManager(
        root=tmp_path / "repos",
        runner=GitCommandRunner(default_timeout_seconds=10),
    )

    workspace = manager.ensure_repository(
        repository_id=repository_id,
        canonical_name="h5vision/vss_server",
        remote_url=str(remote),
        default_branch_ref="refs/heads/main",
    )

    assert workspace.parent == (tmp_path / "repos").resolve()
    assert workspace.name.endswith(repository_id.hex[:8])
    assert "h5vision--vss_server" in workspace.name
    assert _git(workspace, "rev-parse", "HEAD") == first_sha

    (source / "app.py").write_text("version = 2\n", encoding="utf-8")
    _git(source, "add", "--all")
    _git(source, "commit", "-m", "second")
    second_sha = _git(source, "rev-parse", "HEAD")
    _git(source, "push", "origin", "main")

    same_workspace = manager.ensure_repository(
        repository_id=repository_id,
        canonical_name="h5vision/vss_server",
        remote_url=str(remote),
        default_branch_ref="refs/heads/main",
    )

    assert same_workspace == workspace
    assert _git(workspace, "rev-parse", "HEAD") == second_sha


def test_workspace_manager_refuses_dirty_or_unsafe_workspaces(tmp_path: Path) -> None:
    remote, _, _ = _create_remote(tmp_path)
    manager = RepositoryWorkspaceManager(root=tmp_path / "repos")
    repository_id = uuid4()
    workspace = manager.ensure_repository(
        repository_id=repository_id,
        canonical_name="../unsafe/repo",
        remote_url=str(remote),
        default_branch_ref="refs/heads/main",
    )
    assert workspace.parent == (tmp_path / "repos").resolve()

    (workspace / "local-only.txt").write_text("do not overwrite\n", encoding="utf-8")
    with pytest.raises(CollectionError) as exc_info:
        manager.ensure_repository(
            repository_id=repository_id,
            canonical_name="../unsafe/repo",
            remote_url=str(remote),
            default_branch_ref="refs/heads/main",
        )
    assert exc_info.value.reason == "REPOSITORY_WORKSPACE_DIRTY"


def test_workspace_manager_rejects_invalid_default_branch(tmp_path: Path) -> None:
    manager = RepositoryWorkspaceManager(root=tmp_path / "repos")
    with pytest.raises(CollectionError) as exc_info:
        manager.ensure_repository(
            repository_id=uuid4(),
            canonical_name="h5vision/vss_server",
            remote_url="https://example.invalid/repo.git",
            default_branch_ref="main",
        )
    assert exc_info.value.reason == "REPOSITORY_DEFAULT_BRANCH_INVALID"
