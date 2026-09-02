"""선택 Branch만 fetch하고 관측한 HEAD object를 보존하는 Git client 검증."""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

from backend.features.repository_collection.git_client import RepositoryGitClient


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def create_remote(root: Path) -> tuple[Path, Path, str, str]:
    remote = root / "remote.git"
    work = root / "work"
    remote.mkdir()
    work.mkdir()
    git(remote, "init", "--bare")
    git(work, "init", "-b", "main")
    git(work, "config", "user.email", "collector@example.invalid")
    git(work, "config", "user.name", "Collector Test")
    git(work, "remote", "add", "origin", str(remote))
    (work / "app.py").write_text("version = 1\n", "utf-8")
    git(work, "add", "--all")
    git(work, "commit", "-m", "first")
    first_sha = git(work, "rev-parse", "HEAD")
    git(work, "push", "-u", "origin", "main")

    git(work, "checkout", "-b", "feature")
    (work / "feature.py").write_text("enabled = True\n", "utf-8")
    git(work, "add", "--all")
    git(work, "commit", "-m", "feature")
    feature_sha = git(work, "rev-parse", "HEAD")
    git(work, "push", "-u", "origin", "feature")
    git(work, "checkout", "main")
    return remote, work, first_sha, feature_sha


def test_catalog_fetch_history_and_exact_checkout(tmp_path: Path) -> None:
    remote, work, first_sha, feature_sha = create_remote(tmp_path)
    repository_id = uuid4()
    tracked_branch_id = uuid4()
    client = RepositoryGitClient(root=tmp_path / "snapshots")

    catalog = client.list_remote_heads(str(remote))
    assert [(item.branch_ref, item.commit_sha) for item in catalog] == [
        ("refs/heads/feature", feature_sha),
        ("refs/heads/main", first_sha),
    ]

    assert client.fetch_branch(
        repository_id=repository_id,
        tracked_branch_id=tracked_branch_id,
        remote_url=str(remote),
        branch_ref="refs/heads/main",
    ) == first_sha
    cache = tmp_path / "snapshots" / ".repository-cache" / f"{repository_id.hex}.git"
    assert git(cache, "for-each-ref", "--format=%(refname)", "refs/remotes/origin") == (
        "refs/remotes/origin/main"
    )

    (work / "app.py").write_text("version = 2\n", "utf-8")
    git(work, "add", "--all")
    git(work, "commit", "-m", "second")
    second_sha = git(work, "rev-parse", "HEAD")
    git(work, "push", "origin", "main")
    assert client.fetch_branch(
        repository_id=repository_id,
        tracked_branch_id=tracked_branch_id,
        remote_url=str(remote),
        branch_ref="refs/heads/main",
    ) == second_sha
    assert client.is_ancestor(repository_id, first_sha, second_sha) is True

    git(work, "reset", "--hard", first_sha)
    git(work, "push", "--force", "origin", "HEAD:main")
    assert client.fetch_branch(
        repository_id=repository_id,
        tracked_branch_id=tracked_branch_id,
        remote_url=str(remote),
        branch_ref="refs/heads/main",
    ) == first_sha
    assert client.is_ancestor(repository_id, second_sha, first_sha) is False
    assert git(cache, "cat-file", "-t", second_sha) == "commit"
    assert git(
        cache,
        "show-ref",
        "--verify",
        f"refs/vss-history/{tracked_branch_id.hex}/{second_sha}",
    ).startswith(second_sha)

    checkout = tmp_path / "checkout"
    client.checkout_revision(
        repository_id=repository_id,
        revision=second_sha,
        destination=checkout,
    )
    assert git(checkout, "rev-parse", "HEAD") == second_sha
    assert git(checkout, "status", "--porcelain=v1", "--untracked-files=all") == ""
