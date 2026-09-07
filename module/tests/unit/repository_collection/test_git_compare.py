"""Unit tests for RepositoryGitClient.compare_revisions."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from backend.features.repository_collection.errors import CollectionError
from backend.features.repository_collection.git_client import (
    GitCompareFileChange,
    GitCompareResult,
    RepositoryGitClient,
)


def _init_bare_cache(cache_dir: Path) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", str(cache_dir)], check=True, capture_output=True)


def _populate_test_repo(work_dir: Path, bare_dir: Path) -> dict[str, str]:
    """Create commits in a working repo and push to bare cache."""
    work_dir.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Tester",
        "GIT_AUTHOR_EMAIL": "tester@example.com",
        "GIT_COMMITTER_NAME": "Tester",
        "GIT_COMMITTER_EMAIL": "tester@example.com",
    }

    def run_git(*args: str) -> str:
        res = subprocess.run(
            ["git", "-C", str(work_dir), *args],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        return res.stdout.strip()

    run_git("init", "-b", "main")
    run_git("config", "user.name", "Tester")
    run_git("config", "user.email", "tester@example.com")
    run_git("remote", "add", "origin", str(bare_dir))

    # Commit 1: Initial commit with file1.txt, file2.txt
    (work_dir / "file1.txt").write_text("hello\nworld\n", encoding="utf-8")
    (work_dir / "file2.txt").write_text("line1\n", encoding="utf-8")
    run_git("add", ".")
    run_git("commit", "-m", "initial commit")
    sha1 = run_git("rev-parse", "HEAD")

    # Commit 2: Modify file1, delete file2, add file3.txt
    (work_dir / "file1.txt").write_text("hello\nbrave\nnew\nworld\n", encoding="utf-8")
    (work_dir / "file2.txt").unlink()
    (work_dir / "file3.txt").write_text("new file content\n", encoding="utf-8")
    run_git("add", ".")
    run_git("commit", "-m", "second commit")
    sha2 = run_git("rev-parse", "HEAD")

    # Commit 3: Rename file3.txt -> file3_renamed.txt and add a line
    (work_dir / "file3.txt").rename(work_dir / "file3_renamed.txt")
    with open(work_dir / "file3_renamed.txt", "a", encoding="utf-8") as f:
        f.write("extra line\n")
    run_git("add", ".")
    run_git("commit", "-m", "third commit with rename")
    sha3 = run_git("rev-parse", "HEAD")

    # Branch: create a branch from Commit 1, add branch_file.txt
    run_git("checkout", "-b", "feature", sha1)
    (work_dir / "branch_file.txt").write_text("branch\n", encoding="utf-8")
    run_git("add", ".")
    run_git("commit", "-m", "feature branch commit")
    sha_feature = run_git("rev-parse", "HEAD")

    # Push all branches to bare
    run_git("push", "origin", "main", "feature")

    return {
        "sha1": sha1,
        "sha2": sha2,
        "sha3": sha3,
        "sha_feature": sha_feature,
    }


def test_compare_revisions_linear_changes(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    work_dir = tmp_path / "work"
    repo_id = uuid4()
    bare_dir = cache_root / ".repository-cache" / f"{repo_id.hex}.git"
    _init_bare_cache(bare_dir)
    commits = _populate_test_repo(work_dir, bare_dir)

    client = RepositoryGitClient(root=cache_root)

    # Compare sha1 -> sha2
    result = client.compare_revisions(
        repository_id=repo_id,
        base_revision=commits["sha1"],
        target_revision=commits["sha2"],
    )

    assert isinstance(result, GitCompareResult)
    assert result.base_revision == commits["sha1"]
    assert result.target_revision == commits["sha2"]
    assert result.merge_base_revision == commits["sha1"]
    assert result.ahead_count == 1
    assert result.behind_count == 0
    assert result.files_changed == 3
    assert result.additions > 0
    assert result.deletions > 0

    assert all(isinstance(c, GitCompareFileChange) for c in result.changes)
    assert [change.path for change in result.changes] == sorted(
        change.path for change in result.changes
    )
    change_map = {c.path: c for c in result.changes}
    assert "file1.txt" in change_map
    assert change_map["file1.txt"].change_type == "modified"
    assert "file2.txt" in change_map
    assert change_map["file2.txt"].change_type == "deleted"
    assert "file3.txt" in change_map
    assert change_map["file3.txt"].change_type == "added"


def test_compare_revisions_rename(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    work_dir = tmp_path / "work"
    repo_id = uuid4()
    bare_dir = cache_root / ".repository-cache" / f"{repo_id.hex}.git"
    _init_bare_cache(bare_dir)
    commits = _populate_test_repo(work_dir, bare_dir)

    client = RepositoryGitClient(root=cache_root)

    # Compare sha2 -> sha3 (contains rename)
    result = client.compare_revisions(
        repository_id=repo_id,
        base_revision=commits["sha2"],
        target_revision=commits["sha3"],
    )

    assert result.ahead_count == 1
    assert result.behind_count == 0
    assert result.files_changed == 1
    rename_change = result.changes[0]
    assert rename_change.change_type == "renamed"
    assert rename_change.path == "file3_renamed.txt"
    assert rename_change.old_path == "file3.txt"


def test_compare_revisions_divergent_branches(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    work_dir = tmp_path / "work"
    repo_id = uuid4()
    bare_dir = cache_root / ".repository-cache" / f"{repo_id.hex}.git"
    _init_bare_cache(bare_dir)
    commits = _populate_test_repo(work_dir, bare_dir)

    client = RepositoryGitClient(root=cache_root)

    # Compare sha3 (main) vs sha_feature (feature)
    # Common ancestor is sha1
    # sha_feature is 1 ahead of sha1
    # sha3 is 2 ahead of sha1 (sha2, sha3)
    result = client.compare_revisions(
        repository_id=repo_id,
        base_revision=commits["sha3"],
        target_revision=commits["sha_feature"],
    )

    assert result.merge_base_revision == commits["sha1"]
    assert result.ahead_count == 1
    assert result.behind_count == 2


def test_compare_revisions_identical(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    work_dir = tmp_path / "work"
    repo_id = uuid4()
    bare_dir = cache_root / ".repository-cache" / f"{repo_id.hex}.git"
    _init_bare_cache(bare_dir)
    commits = _populate_test_repo(work_dir, bare_dir)

    client = RepositoryGitClient(root=cache_root)

    result = client.compare_revisions(
        repository_id=repo_id,
        base_revision=commits["sha1"],
        target_revision=commits["sha1"],
    )

    assert result.base_revision == commits["sha1"]
    assert result.target_revision == commits["sha1"]
    assert result.merge_base_revision == commits["sha1"]
    assert result.ahead_count == 0
    assert result.behind_count == 0
    assert result.files_changed == 0
    assert result.additions == 0
    assert result.deletions == 0
    assert result.changes == []


def test_compare_revisions_invalid_sha(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    repo_id = uuid4()
    bare_dir = cache_root / ".repository-cache" / f"{repo_id.hex}.git"
    _init_bare_cache(bare_dir)

    client = RepositoryGitClient(root=cache_root)

    with pytest.raises(CollectionError) as exc_info:
        client.compare_revisions(
            repository_id=repo_id,
            base_revision="invalid-sha",
            target_revision="1" * 40,
        )
    assert exc_info.value.reason == "COMPARE_REVISION_INVALID"


def test_compare_revisions_missing_cache(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    repo_id = uuid4()

    client = RepositoryGitClient(root=cache_root)

    with pytest.raises(CollectionError) as exc_info:
        client.compare_revisions(
            repository_id=repo_id,
            base_revision="1" * 40,
            target_revision="2" * 40,
        )
    assert exc_info.value.reason == "REPOSITORY_CACHE_UNAVAILABLE"


def test_compare_revisions_commit_not_found(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    work_dir = tmp_path / "work"
    repo_id = uuid4()
    bare_dir = cache_root / ".repository-cache" / f"{repo_id.hex}.git"
    _init_bare_cache(bare_dir)
    commits = _populate_test_repo(work_dir, bare_dir)

    client = RepositoryGitClient(root=cache_root)

    with pytest.raises(CollectionError) as exc_info:
        client.compare_revisions(
            repository_id=repo_id,
            base_revision=commits["sha1"],
            target_revision="f" * 40,  # Non-existent commit
        )
    assert exc_info.value.reason == "COMPARE_REVISION_NOT_FOUND"
    assert exc_info.value.status_code == 404


def test_compare_revisions_korean_filename(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    work_dir = tmp_path / "work"
    repo_id = uuid4()
    bare_dir = cache_root / ".repository-cache" / f"{repo_id.hex}.git"
    _init_bare_cache(bare_dir)
    commits = _populate_test_repo(work_dir, bare_dir)

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Tester",
        "GIT_AUTHOR_EMAIL": "tester@example.com",
        "GIT_COMMITTER_NAME": "Tester",
        "GIT_COMMITTER_EMAIL": "tester@example.com",
    }
    # Checkout main before adding file
    subprocess.run(["git", "-C", str(work_dir), "checkout", "main"], check=True, env=env)
    (work_dir / "문서").mkdir(exist_ok=True)
    (work_dir / "문서" / "설명서.txt").write_text("한글 내용\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(work_dir), "add", "."], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(work_dir), "commit", "-m", "add korean file"],
        check=True,
        env=env,
    )
    sha_korean = subprocess.run(
        ["git", "-C", str(work_dir), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(work_dir), "push", "origin", "main"], check=True, env=env)

    client = RepositoryGitClient(root=cache_root)
    result = client.compare_revisions(
        repository_id=repo_id,
        base_revision=commits["sha3"],
        target_revision=sha_korean,
    )

    assert result.files_changed == 1
    # Check that Korean path is not mangled or octal-escaped
    assert result.changes[0].path in {"문서/설명서.txt", "문서\\설명서.txt"}
    assert result.changes[0].change_type == "added"

