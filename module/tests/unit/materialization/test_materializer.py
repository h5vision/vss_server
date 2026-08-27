"""Filesystem and Git revision guarantees for Snapshot materialization."""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from backend.features.materialization.errors import MaterializationError
from backend.features.materialization.service import SnapshotMaterializer
from backend.features.materialization.source import GitTreeSource
from backend.features.workspace_overlays.schemas import WorkspaceOverlayRequest


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def create_source_repository(root: Path) -> tuple[Path, str, str]:
    repository = root / "source"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.email", "snapshot@example.invalid")
    git(repository, "config", "user.name", "Snapshot Test")
    (repository / "src").mkdir()
    (repository / "src/current.txt").write_text("base\n", encoding="utf-8")
    (repository / "src/old.txt").write_text("old\n", encoding="utf-8")
    (repository / "src/deleted.txt").write_text("delete\n", encoding="utf-8")
    git(repository, "add", "--all")
    git(repository, "commit", "-m", "base")
    base_revision = git(repository, "rev-parse", "HEAD")

    (repository / "src/current.txt").write_text("target\n", encoding="utf-8")
    (repository / "src/old.txt").unlink()
    (repository / "src/new.txt").write_text("renamed\n", encoding="utf-8")
    (repository / "src/deleted.txt").unlink()
    git(repository, "add", "--all")
    git(repository, "commit", "-m", "target")
    target_revision = git(repository, "rev-parse", "HEAD")
    return repository, base_revision, target_revision


def overlay(base_revision: str, target_revision: str, *, current: str = "target\n"):
    return WorkspaceOverlayRequest.model_validate(
        {
            "project_id": "h5vision/vision",
            "base_revision": base_revision,
            "target_revision": target_revision,
            "files": [
                {
                    "status": "modified",
                    "path": "src/current.txt",
                    "content": current,
                    "encoding": "utf-8",
                },
                {
                    "status": "added",
                    "path": "src/new.txt",
                    "content": "renamed\n",
                    "encoding": "utf-8",
                },
            ],
            "deleted_paths": ["src/deleted.txt"],
            "renames": [{"old_path": "src/old.txt", "new_path": "src/new.txt"}],
        }
    )


def test_materializer_promotes_an_exact_immutable_git_revision(tmp_path: Path) -> None:
    repository, base_revision, target_revision = create_source_repository(tmp_path)
    materializer = SnapshotMaterializer(
        root=tmp_path / "snapshots",
        source=GitTreeSource(command_timeout_seconds=10),
    )

    result = materializer.materialize(
        overlay(base_revision, target_revision),
        binding_id=uuid4(),
        snapshot_id=uuid4(),
        remote_url=str(repository),
        branch_ref="refs/heads/main",
    )

    assert result.project_root.is_dir()
    assert git(result.project_root, "rev-parse", "HEAD") == target_revision
    assert git(result.project_root, "status", "--porcelain") == ""
    assert (result.project_root / "src/current.txt").read_text("utf-8") == "target\n"
    assert not (result.project_root / "src/old.txt").exists()
    assert not (result.project_root / "src/deleted.txt").exists()
    assert result.locator.endswith(f"revisions/{target_revision}")


def test_tree_mismatch_is_rejected_and_staging_is_removed(tmp_path: Path) -> None:
    repository, base_revision, target_revision = create_source_repository(tmp_path)
    materialization_root = tmp_path / "snapshots"
    materializer = SnapshotMaterializer(
        root=materialization_root,
        source=GitTreeSource(command_timeout_seconds=10),
    )

    with pytest.raises(MaterializationError) as captured:
        materializer.materialize(
            overlay(base_revision, target_revision, current="wrong\n"),
            binding_id=uuid4(),
            snapshot_id=uuid4(),
            remote_url=str(repository),
            branch_ref="refs/heads/main",
        )

    assert captured.value.reason == "SNAPSHOT_REVISION_MISMATCH"
    assert not list(materialization_root.glob("*/staging/*"))
    assert not list(materialization_root.glob("*/revisions/*"))


def test_unavailable_target_commit_is_not_replaced_with_an_invented_revision(
    tmp_path: Path,
) -> None:
    repository, base_revision, _ = create_source_repository(tmp_path)
    materializer = SnapshotMaterializer(
        root=tmp_path / "snapshots",
        source=GitTreeSource(command_timeout_seconds=10),
    )

    with pytest.raises(MaterializationError) as captured:
        materializer.materialize(
            overlay(base_revision, "f" * 40),
            binding_id=uuid4(),
            snapshot_id=uuid4(),
            remote_url=str(repository),
            branch_ref="refs/heads/main",
        )

    assert captured.value.reason == "VSS_REVISION_CONTRACT_UNSUPPORTED"


def test_existing_revision_directory_is_never_overwritten(tmp_path: Path) -> None:
    repository, base_revision, target_revision = create_source_repository(tmp_path)
    materializer = SnapshotMaterializer(
        root=tmp_path / "snapshots",
        source=GitTreeSource(command_timeout_seconds=10),
    )
    binding_id = uuid4()
    first = materializer.materialize(
        overlay(base_revision, target_revision),
        binding_id=binding_id,
        snapshot_id=uuid4(),
        remote_url=str(repository),
        branch_ref="refs/heads/main",
    )
    marker = first.project_root / "immutable-marker"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(MaterializationError) as captured:
        materializer.materialize(
            overlay(base_revision, target_revision),
            binding_id=binding_id,
            snapshot_id=uuid4(),
            remote_url=str(repository),
            branch_ref="refs/heads/main",
        )

    assert captured.value.reason == "SNAPSHOT_REVISION_ALREADY_EXISTS"
    assert marker.read_text("utf-8") == "preserve"
