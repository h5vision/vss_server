"""Tests for branch-scoped managed Repository workspaces under SNAPSHOT_REPOSITORY_ROOT."""

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


def _create_remote(root: Path, name: str = "remote") -> tuple[Path, Path, str, str]:
    remote = root / f"{name}.git"
    work = root / f"{name}-source"
    remote.mkdir()
    work.mkdir()
    _git(remote, "init", "--bare")
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.name", "Workspace Test")
    _git(work, "config", "user.email", "workspace@example.invalid")
    _git(work, "remote", "add", "origin", str(remote))
    (work / "app.py").write_text("version = 1\n", encoding="utf-8")
    _git(work, "add", "--all")
    _git(work, "commit", "-m", "main first")
    main_sha = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "-u", "origin", "main")

    _git(work, "checkout", "-b", "feature/login")
    (work / "feature.py").write_text("feature = True\n", encoding="utf-8")
    _git(work, "add", "--all")
    _git(work, "commit", "-m", "feature first")
    feature_sha = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "-u", "origin", "feature/login")
    _git(work, "checkout", "main")
    return remote, work, main_sha, feature_sha


def _manager(root: Path) -> RepositoryWorkspaceManager:
    return RepositoryWorkspaceManager(
        root=root / "repos",
        runner=GitCommandRunner(default_timeout_seconds=10),
    )


def test_workspace_manager_keeps_independent_branch_working_copies(tmp_path: Path) -> None:
    remote, source, main_sha, feature_sha = _create_remote(tmp_path)
    repository_id = uuid4()
    manager = _manager(tmp_path)

    main_workspace = manager.ensure_branch(
        repository_id=repository_id,
        canonical_name="h5vision/vss_server",
        remote_url=str(remote),
        branch_ref="refs/heads/main",
        expected_revision=main_sha,
    )
    feature_workspace = manager.ensure_branch(
        repository_id=repository_id,
        canonical_name="h5vision/vss_server",
        remote_url=str(remote),
        branch_ref="refs/heads/feature/login",
        expected_revision=feature_sha,
    )

    expected_branches = (
        tmp_path / "repos" / ".snapshot-worktrees" / "vss_server" / repository_id.hex / "branches"
    ).resolve()
    assert main_workspace.parent == expected_branches
    assert feature_workspace.parent == expected_branches
    assert main_workspace.name == "main"
    assert feature_workspace.name.startswith("feature-login-")
    assert "--" not in feature_workspace.name
    assert main_workspace != feature_workspace
    assert _git(main_workspace, "rev-parse", "HEAD") == main_sha
    assert _git(feature_workspace, "rev-parse", "HEAD") == feature_sha
    assert _git(main_workspace, "config", "--get", "sol.repository-id") == str(repository_id)
    assert (
        _git(feature_workspace, "config", "--get", "sol.branch-ref") == "refs/heads/feature/login"
    )

    (source / "app.py").write_text("version = 2\n", encoding="utf-8")
    _git(source, "add", "--all")
    _git(source, "commit", "-m", "main second")
    main_second_sha = _git(source, "rev-parse", "HEAD")
    _git(source, "push", "origin", "main")

    same_main_workspace = manager.ensure_branch(
        repository_id=repository_id,
        canonical_name="h5vision/vss_server",
        remote_url=str(remote),
        branch_ref="refs/heads/main",
        expected_revision=main_second_sha,
    )

    assert same_main_workspace == main_workspace
    assert _git(main_workspace, "rev-parse", "HEAD") == main_second_sha
    assert _git(feature_workspace, "rev-parse", "HEAD") == feature_sha


def test_workspace_manager_refuses_dirty_branch_workspace(tmp_path: Path) -> None:
    remote, _, main_sha, _ = _create_remote(tmp_path)
    manager = _manager(tmp_path)
    repository_id = uuid4()
    workspace = manager.ensure_branch(
        repository_id=repository_id,
        canonical_name="../unsafe/vss_server.git",
        remote_url=str(remote),
        branch_ref="refs/heads/main",
        expected_revision=main_sha,
    )
    assert workspace.name == "main"

    (workspace / "local-only.txt").write_text("do not overwrite\n", encoding="utf-8")
    with pytest.raises(CollectionError) as exc_info:
        manager.ensure_branch(
            repository_id=repository_id,
            canonical_name="../unsafe/vss_server.git",
            remote_url=str(remote),
            branch_ref="refs/heads/main",
            expected_revision=main_sha,
        )
    assert exc_info.value.reason == "REPOSITORY_WORKSPACE_DIRTY"


def test_workspace_manager_namespaces_same_basename_repositories_by_id(tmp_path: Path) -> None:
    first_remote, _, first_sha, _ = _create_remote(tmp_path, "first")
    second_remote, _, second_sha, _ = _create_remote(tmp_path, "second")
    manager = _manager(tmp_path)
    first_id = uuid4()
    second_id = uuid4()

    first_workspace = manager.ensure_branch(
        repository_id=first_id,
        canonical_name="org-one/shared.git",
        remote_url=str(first_remote),
        branch_ref="refs/heads/main",
        expected_revision=first_sha,
    )
    second_workspace = manager.ensure_branch(
        repository_id=second_id,
        canonical_name="org-two/shared.git",
        remote_url=str(second_remote),
        branch_ref="refs/heads/main",
        expected_revision=second_sha,
    )

    assert first_workspace != second_workspace
    assert first_workspace.parent.parent.name == first_id.hex
    assert second_workspace.parent.parent.name == second_id.hex
    assert _git(first_workspace, "rev-parse", "HEAD") == first_sha
    assert _git(second_workspace, "rev-parse", "HEAD") == second_sha


def test_workspace_manager_rejects_identity_collision_inside_repository_namespace(
    tmp_path: Path,
) -> None:
    remote, _, main_sha, _ = _create_remote(tmp_path, "owned")
    manager = _manager(tmp_path)
    repository_id = uuid4()

    workspace = manager.ensure_branch(
        repository_id=repository_id,
        canonical_name="org/shared.git",
        remote_url=str(remote),
        branch_ref="refs/heads/main",
        expected_revision=main_sha,
    )
    _git(workspace, "config", "sol.repository-id", str(uuid4()))

    with pytest.raises(CollectionError) as exc_info:
        manager.ensure_branch(
            repository_id=repository_id,
            canonical_name="org/shared.git",
            remote_url=str(remote),
            branch_ref="refs/heads/main",
            refresh_existing=False,
        )

    assert exc_info.value.reason == "REPOSITORY_WORKSPACE_COLLISION"


def test_workspace_manager_disambiguates_sanitized_branch_name_collisions(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    repository_id = uuid4()

    slash_path = manager.workspace_path(
        repository_id=repository_id,
        canonical_name="h5vision/vss_server",
        branch_ref="refs/heads/feature/login",
    )
    literal_separator_path = manager.workspace_path(
        repository_id=repository_id,
        canonical_name="h5vision/vss_server",
        branch_ref="refs/heads/feature--login",
    )

    assert slash_path.name.startswith("feature-login-")
    assert literal_separator_path.name.startswith("feature-login-")
    assert "--" not in slash_path.name
    assert "--" not in literal_separator_path.name
    assert literal_separator_path != slash_path


def test_workspace_manager_reserves_double_hyphen_for_vss_index_names(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    repository_id = uuid4()

    workspace = manager.workspace_path(
        repository_id=repository_id,
        canonical_name="h5vision/repo--name.git",
        branch_ref="refs/heads/release--candidate",
    )
    relative = workspace.relative_to((tmp_path / "repos").resolve())

    assert relative.parts[0] == ".snapshot-worktrees"
    assert relative.parts[1] == "repo-name"
    assert relative.parts[2] == repository_id.hex
    assert relative.parts[3] == "branches"
    assert all("--" not in component for component in relative.parts)


def test_workspace_manager_rejects_stale_expected_revision(tmp_path: Path) -> None:
    remote, _, _, _ = _create_remote(tmp_path)
    manager = _manager(tmp_path)

    with pytest.raises(CollectionError) as exc_info:
        manager.ensure_branch(
            repository_id=uuid4(),
            canonical_name="h5vision/vss_server",
            remote_url=str(remote),
            branch_ref="refs/heads/main",
            expected_revision="1" * 40,
        )
    assert exc_info.value.reason == "REPOSITORY_BRANCH_HEAD_MISMATCH"
    assert exc_info.value.retryable is True


def test_workspace_manager_can_inspect_existing_workspace_without_refreshing_remote_head(
    tmp_path: Path,
) -> None:
    remote, source, main_sha, _ = _create_remote(tmp_path)
    manager = _manager(tmp_path)
    repository_id = uuid4()
    workspace = manager.ensure_branch(
        repository_id=repository_id,
        canonical_name="h5vision/vss_server",
        remote_url=str(remote),
        branch_ref="refs/heads/main",
        expected_revision=main_sha,
    )

    (source / "app.py").write_text("version = 3\n", encoding="utf-8")
    _git(source, "add", "--all")
    _git(source, "commit", "-m", "main third")
    new_remote_sha = _git(source, "rev-parse", "HEAD")
    _git(source, "push", "origin", "main")
    assert new_remote_sha != main_sha

    same_workspace = manager.ensure_branch(
        repository_id=repository_id,
        canonical_name="h5vision/vss_server",
        remote_url=str(remote),
        branch_ref="refs/heads/main",
        expected_revision=new_remote_sha,
        refresh_existing=False,
    )

    assert same_workspace == workspace
    assert _git(workspace, "rev-parse", "HEAD") == main_sha


def test_workspace_manager_rejects_invalid_branch_ref(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    with pytest.raises(CollectionError) as exc_info:
        manager.ensure_branch(
            repository_id=uuid4(),
            canonical_name="h5vision/vss_server",
            remote_url="https://example.invalid/repo.git",
            branch_ref="main",
        )
    assert exc_info.value.reason == "REPOSITORY_BRANCH_INVALID"
