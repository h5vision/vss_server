"""VSS가 Snapshot SHA와 Git 정합성 증거를 pull하는 내부 API를 검증한다."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app import create_app
from backend.core.config import Settings
from backend.infrastructure.database.base import Base
from backend.infrastructure.database.models import (
    BranchBinding,
    Repository,
    Snapshot,
    TrackedBranch,
)


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def seed_materialized_snapshot(database_path: Path, materialization_root: Path) -> tuple[str, str]:
    source = materialization_root.parent / "source"
    source.mkdir()
    git(source, "init", "-b", "module")
    git(source, "config", "user.email", "snapshot@example.invalid")
    git(source, "config", "user.name", "Snapshot Test")
    git(source, "config", "core.autocrlf", "false")
    (source / "app.py").write_text("VERSION = 1\n", encoding="utf-8")
    git(source, "add", "--all")
    git(source, "commit", "-m", "base")
    base_revision = git(source, "rev-parse", "HEAD")
    (source / "app.py").write_text("VERSION = 2\n", encoding="utf-8")
    git(source, "add", "--all")
    git(source, "commit", "-m", "target")
    target_revision = git(source, "rev-parse", "HEAD")

    engine = create_engine(
        f"sqlite:///{database_path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        repository = Repository(
            repository_id=uuid4(),
            canonical_name="h5vision/vss_server",
            display_name="VSS Server",
            provider="github",
            remote_url="https://github.com/h5vision/vss_server.git",
            default_branch_ref="refs/heads/module",
        )
        session.add(repository)
        session.flush()
        binding = BranchBinding(
            frontend_project_id="legacy-not-used-by-vss",
            repository_id=repository.repository_id,
            branch_ref="refs/heads/module",
            vss_project_id="vss-server--module",
            active=True,
        )
        session.add(binding)
        session.flush()
        revision_root = (
            materialization_root / binding.binding_id.hex / "revisions" / target_revision
        )
        revision_root.parent.mkdir(parents=True)
        shutil.move(str(source), str(revision_root))
        snapshot = Snapshot(
            request_id=uuid4(),
            binding_id=binding.binding_id,
            frontend_project_id="legacy-not-used-by-vss",
            repository_id=repository.repository_id,
            branch_ref=binding.branch_ref,
            vss_project_id=binding.vss_project_id,
            base_revision=base_revision,
            target_revision=target_revision,
            source_type="remote_clone",
            state="materialized",
            materialized_locator=revision_root.relative_to(materialization_root).as_posix(),
        )
        session.add(snapshot)
        session.commit()
    engine.dispose()
    return target_revision, git(revision_root, "rev-parse", "HEAD^{tree}")


def test_vss_can_pull_verified_source_and_revision_history(tmp_path: Path) -> None:
    database_path = tmp_path / "snapshot.db"
    materialization_root = tmp_path / "snapshots"
    target_revision, tree_sha = seed_materialized_snapshot(
        database_path,
        materialization_root,
    )
    app = create_app(
        Settings(
            vision_environment="test",
            database_url=f"sqlite+aiosqlite:///{database_path}",
            snapshot_materialization_root=materialization_root,
            snapshot_vss_api_token="shared-secret",
            snapshot_recovery_on_startup=False,
            docs_enabled=False,
        )
    )

    with TestClient(app) as client:
        source = client.get(
            "/v1/internal/vss/source",
            params={"project_id": "vss-server--module"},
            headers={"X-Snapshot-Token": "shared-secret"},
        )
        history = client.get(
            "/v1/internal/vss/revisions",
            params={"project_id": "vss-server--module"},
            headers={"Authorization": "Bearer shared-secret"},
        )

    assert source.status_code == 200, source.text
    body = source.json()
    assert body["schema_version"] == "1.0"
    assert body["reason"] == "VSS_SOURCE_READY"
    assert body["target_revision"] == target_revision
    assert body["verification"]["expected_commit_sha"] == target_revision
    assert body["verification"]["expected_tree_sha"] == tree_sha
    assert body["verification"]["working_tree_clean"] is True
    assert body["verification"]["verification_commands"] == [
        "git rev-parse HEAD",
        "git rev-parse HEAD^{tree}",
        "git status --porcelain=v1 --untracked-files=all",
    ]
    assert body["index_request"]["project_id"] == "vss-server--module"
    assert body["index_request"]["project_root"].endswith(target_revision)
    assert body["index_request"]["force"] is False
    assert history.status_code == 200
    assert history.json()["items"][0]["target_revision"] == target_revision
    assert history.json()["items"][0]["materialized"] is True


def test_vss_source_api_requires_a_separate_inbound_token(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            vision_environment="test",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}",
            snapshot_materialization_root=tmp_path / "snapshots",
            snapshot_vss_api_token="shared-secret",
            snapshot_recovery_on_startup=False,
            docs_enabled=False,
        )
    )
    with TestClient(app) as client:
        response = client.get(
            "/v1/internal/vss/source",
            params={"project_id": "vss-server--module"},
            headers={"X-Snapshot-Token": "wrong-token"},
        )
    assert response.status_code == 401
    assert response.json()["reason"] == "VSS_SOURCE_AUTH_REQUIRED"
    assert "wrong-token" not in response.text


def test_vss_can_pull_collector_owned_snapshot_without_frontend_binding(tmp_path: Path) -> None:
    database_path = tmp_path / "collector.db"
    materialization_root = tmp_path / "snapshots"
    source = tmp_path / "collector-source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.email", "collector@example.invalid")
    git(source, "config", "user.name", "Collector Test")
    git(source, "config", "core.autocrlf", "false")
    (source / "main.py").write_text("COLLECTED = True\n", "utf-8")
    git(source, "add", "--all")
    git(source, "commit", "-m", "collected")
    target_revision = git(source, "rev-parse", "HEAD")

    engine = create_engine(
        f"sqlite:///{database_path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        repository = Repository(
            canonical_name="h5vision/collector",
            display_name="Collector",
            provider="github",
            remote_url="https://github.com/h5vision/collector.git",
            default_branch_ref="refs/heads/main",
        )
        session.add(repository)
        session.flush()
        tracked_branch = TrackedBranch(
            repository_id=repository.repository_id,
            branch_ref="refs/heads/main",
            vss_project_id="collector--main",
            current_head_sha=target_revision,
        )
        session.add(tracked_branch)
        session.flush()
        revision_root = (
            materialization_root
            / tracked_branch.tracked_branch_id.hex
            / "revisions"
            / target_revision
        )
        revision_root.parent.mkdir(parents=True)
        shutil.move(str(source), str(revision_root))
        session.add(
            Snapshot(
                request_id=uuid4(),
                binding_id=None,
                tracked_branch_id=tracked_branch.tracked_branch_id,
                frontend_project_id=None,
                repository_id=repository.repository_id,
                branch_ref=tracked_branch.branch_ref,
                vss_project_id=tracked_branch.vss_project_id,
                base_revision=target_revision,
                target_revision=target_revision,
                source_type="remote_clone",
                state="materialized",
                materialized_locator=revision_root.relative_to(
                    materialization_root
                ).as_posix(),
            )
        )
        session.commit()
    engine.dispose()

    app = create_app(
        Settings(
            vision_environment="test",
            database_url=f"sqlite+aiosqlite:///{database_path}",
            snapshot_materialization_root=materialization_root,
            snapshot_vss_api_token="shared-secret",
            snapshot_recovery_on_startup=False,
            docs_enabled=False,
        )
    )
    with TestClient(app) as client:
        response = client.get(
            "/v1/internal/vss/source",
            params={"project_id": "collector--main"},
            headers={"X-Snapshot-Token": "shared-secret"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["target_revision"] == target_revision
    assert response.json()["branch_ref"] == "refs/heads/main"
    assert response.json()["verification"]["expected_commit_sha"] == target_revision
