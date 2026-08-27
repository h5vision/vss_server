"""Same-Snapshot retry integration without exposing an unauthenticated route."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import httpx2
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from backend.features.indexing.retry import SnapshotRetryService
from backend.features.materialization.service import SnapshotMaterializer
from backend.features.materialization.source import GitTreeSource
from backend.features.workspace_overlays.schemas import WorkspaceOverlayRequest
from backend.infrastructure.database.base import Base
from backend.infrastructure.database.engine import create_engine_from_url, create_sessionmaker
from backend.infrastructure.database.models import (
    BranchBinding,
    Repository,
    Snapshot,
    SnapshotAttempt,
)
from backend.integrations.vss.client import VssHttpClient


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def prepare_snapshot(tmp_path: Path) -> tuple[Path, str, str]:
    repository_path = tmp_path / "repository"
    repository_path.mkdir()
    git(repository_path, "init", "-b", "main")
    git(repository_path, "config", "user.email", "snapshot@example.invalid")
    git(repository_path, "config", "user.name", "Snapshot Test")
    (repository_path / "app.py").write_text("VERSION = 1\n", encoding="utf-8")
    git(repository_path, "add", "--all")
    git(repository_path, "commit", "-m", "base")
    base_revision = git(repository_path, "rev-parse", "HEAD")
    (repository_path / "app.py").write_text("VERSION = 2\n", encoding="utf-8")
    git(repository_path, "add", "--all")
    git(repository_path, "commit", "-m", "target")
    target_revision = git(repository_path, "rev-parse", "HEAD")

    database_path = tmp_path / "snapshot.db"
    sync_engine = create_engine(
        f"sqlite:///{database_path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    Base.metadata.create_all(sync_engine)
    binding_id = uuid4()
    snapshot_id = uuid4()
    request_id = uuid4()
    materializer = SnapshotMaterializer(
        root=tmp_path / "snapshots",
        source=GitTreeSource(command_timeout_seconds=10),
    )
    request = WorkspaceOverlayRequest.model_validate(
        {
            "project_id": "h5vision/example",
            "base_revision": base_revision,
            "target_revision": target_revision,
            "files": [
                {
                    "status": "modified",
                    "path": "app.py",
                    "content": "VERSION = 2\n",
                    "encoding": "utf-8",
                }
            ],
            "deleted_paths": [],
            "renames": [],
        }
    )
    materialized = materializer.materialize(
        request,
        binding_id=binding_id,
        snapshot_id=snapshot_id,
        remote_url=str(repository_path),
        branch_ref="refs/heads/main",
    )

    with Session(sync_engine) as session:
        repository = Repository(
            repository_id=uuid4(),
            canonical_name="h5vision/example",
            display_name="Example",
            provider="git",
            remote_url=str(repository_path),
            default_branch_ref="refs/heads/main",
        )
        session.add(repository)
        session.flush()
        session.add(
            BranchBinding(
                binding_id=binding_id,
                frontend_project_id="h5vision/example",
                frontend_workspace_name="example",
                repository_id=repository.repository_id,
                branch_ref="refs/heads/main",
                vss_project_id="example--main",
                active=True,
            )
        )
        session.flush()
        session.add(
            Snapshot(
                snapshot_id=snapshot_id,
                request_id=request_id,
                binding_id=binding_id,
                frontend_project_id="h5vision/example",
                repository_id=repository.repository_id,
                branch_ref="refs/heads/main",
                vss_project_id="example--main",
                base_revision=base_revision,
                target_revision=target_revision,
                source_type="remote_clone",
                state="failed",
                attempt_count=1,
                materialized_locator=materialized.locator,
                vss_reason="VSS_HTTP_UNAVAILABLE",
            )
        )
        session.flush()
        session.add(
            SnapshotAttempt(
                snapshot_id=snapshot_id,
                request_id=request_id,
                attempt_number=1,
                upstream_status_code=503,
                vss_reason="VSS_HTTP_UNAVAILABLE",
                retryable=True,
            )
        )
        session.commit()
    sync_engine.dispose()
    return database_path, str(snapshot_id), target_revision


def test_retry_reuses_exact_tree_and_adds_only_an_attempt(tmp_path: Path) -> None:
    database_path, snapshot_id, target_revision = prepare_snapshot(tmp_path)
    submitted: list[dict] = []

    def fake_vss(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/index/status":
            return httpx2.Response(
                200,
                json={
                    "project_id": "example--main",
                    "state": "failed",
                    "error": "redacted upstream failure",
                },
            )
        if request.url.path == "/index":
            body = json.loads(request.content)
            submitted.append(body)
            assert Path(body["project_root"]).is_dir()
            assert git(Path(body["project_root"]), "rev-parse", "HEAD") == target_revision
            return httpx2.Response(
                202,
                json={
                    "accepted": True,
                    "project_id": "example--main",
                    "state": "running",
                },
            )
        raise AssertionError(f"unexpected VSS path: {request.url.path}")

    async def scenario() -> None:
        engine = create_engine_from_url(f"sqlite+aiosqlite:///{database_path}")
        sessionmaker = create_sessionmaker(engine)
        client = VssHttpClient(
            base_url="http://vss.example:8200",
            transport=httpx2.MockTransport(fake_vss),
        )
        outcome = await SnapshotRetryService(
            sessionmaker=sessionmaker,
            materializer=SnapshotMaterializer(
                root=tmp_path / "snapshots",
                source=GitTreeSource(command_timeout_seconds=10),
            ),
            vss_client=client,
        ).retry(UUID(snapshot_id), request_id=uuid4())
        assert outcome.status_code == 202
        assert outcome.body.reason == "VSS_INDEX_RETRY_ACCEPTED"
        assert outcome.body.attempt_count == 2
        client.close()
        await engine.dispose()

    asyncio.run(scenario())

    assert len(submitted) == 1
    assert submitted[0]["force"] is False
    assert "snapshot_id" not in submitted[0]

    engine = create_engine(
        f"sqlite:///{database_path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    with Session(engine) as session:
        snapshot = session.scalar(select(Snapshot))
        assert snapshot is not None
        assert snapshot.state == "accepted"
        assert snapshot.attempt_count == 2
        assert session.scalar(select(func.count()).select_from(Snapshot)) == 1
        assert session.scalar(select(func.count()).select_from(SnapshotAttempt)) == 2
    engine.dispose()


def test_retry_does_not_submit_when_active_index_already_matches(tmp_path: Path) -> None:
    database_path, snapshot_id, target_revision = prepare_snapshot(tmp_path)
    seen: list[str] = []

    def fake_vss(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        if request.url.path == "/index/status":
            return httpx2.Response(
                200,
                json={"project_id": "example--main", "state": "none"},
            )
        if request.url.path == "/index/exists":
            return httpx2.Response(
                200,
                json={
                    "project_id": "example--main",
                    "exists": True,
                    "chunks": 8,
                    "commit": target_revision,
                },
            )
        raise AssertionError(f"unexpected VSS path: {request.url.path}")

    async def scenario() -> None:
        engine = create_engine_from_url(f"sqlite+aiosqlite:///{database_path}")
        sessionmaker = create_sessionmaker(engine)
        client = VssHttpClient(
            base_url="http://vss.example:8200",
            transport=httpx2.MockTransport(fake_vss),
        )
        outcome = await SnapshotRetryService(
            sessionmaker=sessionmaker,
            materializer=SnapshotMaterializer(
                root=tmp_path / "snapshots",
                source=GitTreeSource(command_timeout_seconds=10),
            ),
            vss_client=client,
        ).retry(UUID(snapshot_id), request_id=uuid4())
        assert outcome.status_code == 200
        assert outcome.body.reason == "TARGET_ALREADY_INDEXED"
        assert outcome.body.attempt_count == 1
        client.close()
        await engine.dispose()

    asyncio.run(scenario())

    assert seen == ["/index/status", "/index/exists"]

    engine = create_engine(
        f"sqlite:///{database_path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    with Session(engine) as session:
        snapshot = session.scalar(select(Snapshot))
        assert snapshot is not None
        assert snapshot.state == "completed"
        assert snapshot.attempt_count == 1
        assert session.scalar(select(func.count()).select_from(SnapshotAttempt)) == 1
    engine.dispose()
