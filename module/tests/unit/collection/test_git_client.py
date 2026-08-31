"""Unit tests for GitCollectionClient."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.features.collection.errors import CollectionError
from backend.features.collection.git_client import GitCollectionClient


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def init_source_repo(path: Path) -> tuple[str, str]:
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test User")
    (path / "file1.txt").write_text("hello world", "utf-8")
    git(path, "add", "--all")
    git(path, "commit", "-m", "init commit")
    c1 = git(path, "rev-parse", "HEAD")

    git(path, "checkout", "-b", "feature")
    (path / "file2.txt").write_text("feature content", "utf-8")
    git(path, "add", "--all")
    git(path, "commit", "-m", "feature commit")
    c2 = git(path, "rev-parse", "HEAD")

    git(path, "checkout", "main")
    return c1, c2


def test_git_client_remote_heads(tmp_path: Path) -> None:
    source_dir = tmp_path / "source_repo"
    source_dir.mkdir()
    c1, c2 = init_source_repo(source_dir)

    client = GitCollectionClient(command_timeout_seconds=10.0)
    heads = client.remote_heads(str(source_dir.resolve()))

    assert "refs/heads/main" in heads
    assert heads["refs/heads/main"] == c1
    assert "refs/heads/feature" in heads
    assert heads["refs/heads/feature"] == c2


def test_git_client_remote_heads_invalid_url(tmp_path: Path) -> None:
    client = GitCollectionClient(command_timeout_seconds=5.0)
    with pytest.raises(CollectionError) as exc_info:
        client.remote_heads(str(tmp_path / "non_existent"))
    assert exc_info.value.reason == "COLLECTION_REMOTE_UNAVAILABLE"


def test_git_client_mirror_and_checkout(tmp_path: Path) -> None:
    source_dir = tmp_path / "source_repo"
    source_dir.mkdir()
    c1, c2 = init_source_repo(source_dir)

    client = GitCollectionClient(command_timeout_seconds=10.0)
    mirror_dir = tmp_path / "mirror.git"

    # 1. Ensure mirror (clone)
    client.ensure_mirror(str(source_dir), mirror_dir)
    assert mirror_dir.exists()

    # 2. Query head_sha
    main_sha = client.head_sha(mirror_dir, "refs/heads/main")
    assert main_sha == c1
    feat_sha = client.head_sha(mirror_dir, "refs/heads/feature")
    assert feat_sha == c2

    # 3. is_ancestor
    assert client.is_ancestor(mirror_dir, c1, c2) is True
    assert client.is_ancestor(mirror_dir, c2, c1) is False

    # 4. Checkout tree
    dest_dir = tmp_path / "dest_tree"
    client.checkout_tree(mirror_dir, c2, dest_dir)
    assert (dest_dir / "file1.txt").read_text("utf-8") == "hello world"
    assert (dest_dir / "file2.txt").read_text("utf-8") == "feature content"

    # 5. Second ensure_mirror runs fetch
    client.ensure_mirror(str(source_dir), mirror_dir)

