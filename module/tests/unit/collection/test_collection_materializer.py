"""Unit tests for CollectionMaterializer."""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from backend.features.collection.git_client import GitCollectionClient
from backend.features.collection.materializer import CollectionMaterializer
from backend.features.materialization.errors import MaterializationError


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def init_source_repo(path: Path) -> str:
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test User")
    (path / "src").mkdir()
    (path / "src/index.ts").write_text("console.log('hello');\n", "utf-8")
    git(path, "add", "--all")
    git(path, "commit", "-m", "init")
    return git(path, "rev-parse", "HEAD")


def test_collection_materializer_promotes_revision(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    commit_sha = init_source_repo(source_dir)

    git_client = GitCollectionClient(command_timeout_seconds=10.0)
    mirror_dir = tmp_path / "mirror.git"
    git_client.ensure_mirror(str(source_dir), mirror_dir)

    root = tmp_path / "snapshots"
    materializer = CollectionMaterializer(root=root, git=git_client)

    owner_id = uuid4()
    snapshot_id = uuid4()

    result = materializer.materialize(
        owner_id=owner_id,
        snapshot_id=snapshot_id,
        mirror_dir=mirror_dir,
        revision=commit_sha,
    )

    assert result.project_root.exists()
    assert (result.project_root / "src/index.ts").read_text("utf-8") == "console.log('hello');\n"
    assert result.locator == f"{owner_id.hex}/revisions/{commit_sha}"

    # Staging path should be cleaned up
    staging_path = root / str(owner_id) / "staging" / str(snapshot_id)
    assert not staging_path.exists()


def test_collection_materializer_invalid_revision(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    init_source_repo(source_dir)

    git_client = GitCollectionClient(command_timeout_seconds=10.0)
    mirror_dir = tmp_path / "mirror.git"
    git_client.ensure_mirror(str(source_dir), mirror_dir)

    root = tmp_path / "snapshots"
    materializer = CollectionMaterializer(root=root, git=git_client)

    owner_id = uuid4()
    snapshot_id = uuid4()

    with pytest.raises(MaterializationError) as exc_info:
        materializer.materialize(
            owner_id=owner_id,
            snapshot_id=snapshot_id,
            mirror_dir=mirror_dir,
            revision="0" * 40,
        )
    assert exc_info.value.reason in (
        "VSS_REVISION_CONTRACT_UNSUPPORTED",
        "SNAPSHOT_MATERIALIZATION_FAILED",
    )
