"""Frontend overlay to DB, immutable Git tree, and fake VSS integration."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from uuid import uuid4

import httpx2
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from backend.app import create_app
from backend.core.config import Settings
from backend.infrastructure.database.base import Base
from backend.infrastructure.database.models import (
    BranchBinding,
    Repository,
    Snapshot,
    SnapshotAttempt,
    SnapshotDelta,
)


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def create_repository(root: Path) -> tuple[Path, str, str]:
    repository = root / "repository"
    repository.mkdir()
    git(repository, "init", "-b", "frontend")
    git(repository, "config", "user.email", "snapshot@example.invalid")
    git(repository, "config", "user.name", "Snapshot Test")
    (repository / "vision/src").mkdir(parents=True)
    (repository / "vision/src/app.ts").write_text("const version = 1;\n", "utf-8")
    git(repository, "add", "--all")
    git(repository, "commit", "-m", "base")
    base_revision = git(repository, "rev-parse", "HEAD")

    (repository / "vision/src/app.ts").write_text("const version = 2;\n", "utf-8")
    (repository / "vision/src/new.ts").write_text("export const ready = true;\n", "utf-8")
    git(repository, "add", "--all")
    git(repository, "commit", "-m", "target")
    return repository, base_revision, git(repository, "rev-parse", "HEAD")


def database_engine(database_path: Path):
    return create_engine(
        f"sqlite:///{database_path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )


def seed_binding(database_path: Path, repository_path: Path) -> None:
    engine = database_engine(database_path)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        repository = Repository(
            repository_id=uuid4(),
            canonical_name="h5vision/vision",
            display_name="Vision",
            provider="git",
            remote_url=str(repository_path),
            default_branch_ref="refs/heads/frontend",
        )
        session.add(repository)
        session.flush()
        session.add(
            BranchBinding(
                frontend_project_id="h5vision/vision",
                frontend_workspace_name="vision",
                repository_id=repository.repository_id,
                branch_ref="refs/heads/frontend",
                vss_project_id="vision--frontend",
                active=True,
            )
        )
        session.commit()
    engine.dispose()


def payload(base_revision: str, target_revision: str) -> dict:
    return {
        "project_id": "h5vision/vision",
        "base_revision": base_revision,
        "target_revision": target_revision,
        "files": [
            {
                "status": "modified",
                "path": "vision/src/app.ts",
                "content": "const version = 2;\n",
                "encoding": "utf-8",
            },
            {
                "status": "added",
                "path": "vision/src/new.ts",
                "content": "export const ready = true;\n",
                "encoding": "utf-8",
            },
        ],
        "deleted_paths": [],
        "renames": [],
    }


def test_overlay_is_persisted_materialized_and_submitted_once(tmp_path: Path) -> None:
    repository, base_revision, target_revision = create_repository(tmp_path)
    database_path = tmp_path / "snapshot.db"
    materialization_root = tmp_path / "snapshots"
    seed_binding(database_path, repository)
    calls: list[dict] = []

    def fake_vss(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "POST"
        assert request.url.path == "/index"
        body = json.loads(request.content)
        calls.append(body)
        project_root = Path(body["project_root"])
        assert project_root.is_dir()
        assert git(project_root, "rev-parse", "HEAD") == target_revision
        assert git(project_root, "status", "--porcelain") == ""
        assert "files" not in body
        assert "target_revision" not in body
        return httpx2.Response(
            202,
            json={
                "accepted": True,
                "project_id": "vision--frontend",
                "state": "running",
                "fingerprint": {"use_bm25": True},
            },
        )

    app = create_app(
        Settings(
            vision_environment="test",
            database_url=f"sqlite+aiosqlite:///{database_path}",
            snapshot_materialization_root=materialization_root,
            vss_base_url="http://vss.example:8200",
            docs_enabled=False,
        ),
        vss_transport=httpx2.MockTransport(fake_vss),
    )

    with TestClient(app) as client:
        accepted = client.post(
            "/v1/workspace-overlays",
            json=payload(base_revision, target_revision),
        )
        duplicate = client.post(
            "/v1/workspace-overlays",
            json=payload(base_revision, target_revision),
        )

    assert accepted.status_code == 202
    assert accepted.json()["reason"] == "VSS_INDEX_ACCEPTED"
    assert accepted.json()["state"] == "accepted"
    assert accepted.json()["request_id"] == accepted.headers["X-Request-ID"]
    assert str(materialization_root) not in accepted.text
    assert duplicate.status_code == 409
    assert duplicate.json()["reason"] == "SNAPSHOT_ALREADY_EXISTS"
    assert len(calls) == 1

    engine = database_engine(database_path)
    with Session(engine) as session:
        snapshot = session.scalar(select(Snapshot))
        assert snapshot is not None
        assert snapshot.state == "accepted"
        assert snapshot.attempt_count == 1
        assert snapshot.materialized_locator is not None
        assert not Path(snapshot.materialized_locator).is_absolute()
        assert session.scalar(select(func.count()).select_from(Snapshot)) == 1
        assert session.scalar(select(func.count()).select_from(SnapshotDelta)) == 2
        assert session.scalar(select(func.count()).select_from(SnapshotAttempt)) == 1
        attempt = session.scalar(select(SnapshotAttempt))
        assert attempt is not None
        assert attempt.upstream_status_code == 202
        assert "path" not in (attempt.vss_result_json or {})
    engine.dispose()


def test_missing_binding_stops_before_filesystem_and_vss(tmp_path: Path) -> None:
    database_path = tmp_path / "snapshot.db"
    engine = database_engine(database_path)
    Base.metadata.create_all(engine)
    engine.dispose()
    materialization_root = tmp_path / "snapshots"

    def forbidden_vss(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError(f"VSS must not be called: {request.url}")

    app = create_app(
        Settings(
            vision_environment="test",
            database_url=f"sqlite+aiosqlite:///{database_path}",
            snapshot_materialization_root=materialization_root,
            docs_enabled=False,
        ),
        vss_transport=httpx2.MockTransport(forbidden_vss),
    )
    request_payload = payload("1" * 40, "2" * 40)
    request_payload["project_id"] = "unbound/project"

    with TestClient(app) as client:
        response = client.post("/v1/workspace-overlays", json=request_payload)

    assert response.status_code == 409
    assert response.json()["reason"] == "SNAPSHOT_DESTINATION_REQUIRED"
    assert not materialization_root.exists()


def test_vss_already_running_is_persisted_with_a_safe_reason(tmp_path: Path) -> None:
    repository, base_revision, target_revision = create_repository(tmp_path)
    database_path = tmp_path / "snapshot.db"
    seed_binding(database_path, repository)

    def busy_vss(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            409,
            json={
                "accepted": False,
                "reason": "already_running",
                "project_id": "vision--frontend",
                "heartbeat_age_s": 1.5,
                "path": "/srv/private/project",
            },
        )

    app = create_app(
        Settings(
            vision_environment="test",
            database_url=f"sqlite+aiosqlite:///{database_path}",
            snapshot_materialization_root=tmp_path / "snapshots",
            docs_enabled=False,
        ),
        vss_transport=httpx2.MockTransport(busy_vss),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/workspace-overlays",
            json=payload(base_revision, target_revision),
        )

    assert response.status_code == 409
    assert response.json()["reason"] == "VSS_INDEX_ALREADY_RUNNING"
    assert response.json()["retryable"] is True
    assert "/srv/private" not in response.text

    engine = database_engine(database_path)
    with Session(engine) as session:
        snapshot = session.scalar(select(Snapshot))
        assert snapshot is not None
        assert snapshot.state == "rejected"
        assert snapshot.vss_reason == "already_running"
    engine.dispose()


def test_revision_mismatch_fails_before_vss_submission(tmp_path: Path) -> None:
    repository, base_revision, target_revision = create_repository(tmp_path)
    database_path = tmp_path / "snapshot.db"
    seed_binding(database_path, repository)

    def forbidden_vss(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError(f"VSS must not be called: {request.url}")

    app = create_app(
        Settings(
            vision_environment="test",
            database_url=f"sqlite+aiosqlite:///{database_path}",
            snapshot_materialization_root=tmp_path / "snapshots",
            docs_enabled=False,
        ),
        vss_transport=httpx2.MockTransport(forbidden_vss),
    )
    invalid_overlay = payload(base_revision, target_revision)
    invalid_overlay["files"][0]["content"] = "const version = 999;\n"

    with TestClient(app) as client:
        response = client.post("/v1/workspace-overlays", json=invalid_overlay)

    assert response.status_code == 409
    assert response.json()["reason"] == "SNAPSHOT_REVISION_MISMATCH"

    engine = database_engine(database_path)
    with Session(engine) as session:
        snapshot = session.scalar(select(Snapshot))
        assert snapshot is not None
        assert snapshot.state == "failed"
        assert snapshot.attempt_count == 0
    engine.dispose()


def test_vss_not_a_directory_is_an_internal_materialization_failure(tmp_path: Path) -> None:
    repository, base_revision, target_revision = create_repository(tmp_path)
    database_path = tmp_path / "snapshot.db"
    seed_binding(database_path, repository)

    def not_a_directory(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            409,
            json={
                "accepted": False,
                "reason": "not_a_directory",
                "path": "/srv/private/missing",
            },
        )

    app = create_app(
        Settings(
            vision_environment="test",
            database_url=f"sqlite+aiosqlite:///{database_path}",
            snapshot_materialization_root=tmp_path / "snapshots",
            docs_enabled=False,
        ),
        vss_transport=httpx2.MockTransport(not_a_directory),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/workspace-overlays",
            json=payload(base_revision, target_revision),
        )

    assert response.status_code == 500
    assert response.json()["reason"] == "SNAPSHOT_MATERIALIZATION_FAILED"
    assert "/srv/private" not in response.text

    engine = database_engine(database_path)
    with Session(engine) as session:
        snapshot = session.scalar(select(Snapshot))
        assert snapshot is not None
        assert snapshot.state == "failed"
        assert snapshot.vss_reason == "not_a_directory"
    engine.dispose()
