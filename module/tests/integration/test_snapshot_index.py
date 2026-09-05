"""Admin explicit Index가 materialized Snapshot만 VSS에 제출하는지 검증한다."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import httpx2
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from backend.core.errors import ApiError
from backend.features.indexing.index import SnapshotIndexService
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


def prepare_materialized_snapshot(tmp_path: Path) -> tuple[Path, str, str]:
    repository_path = tmp_path / "repository"
    repository_path.mkdir()
    git(repository_path, "init", "-b", "main")
    git(repository_path, "config", "user.email", "snapshot@example.invalid")
    git(repository_path, "config", "user.name", "Snapshot Index Test")
    (repository_path / "app.py").write_text("VERSION = 1\n", encoding="utf-8")
    git(repository_path, "add", "--all")
    git(repository_path, "commit", "-m", "base")
    base_revision = git(repository_path, "rev-parse", "HEAD")
    (repository_path / "app.py").write_text("VERSION = 2\n", encoding="utf-8")
    git(repository_path, "add", "--all")
    git(repository_path, "commit", "-m", "target")
    target_revision = git(repository_path, "rev-parse", "HEAD")

    database_path = tmp_path / "snapshot-index.db"
    sync_engine = create_engine(
        f"sqlite:///{database_path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    Base.metadata.create_all(sync_engine)
    binding_id = uuid4()
    snapshot_id = uuid4()
    materializer = SnapshotMaterializer(
        root=tmp_path / "snapshots",
        source=GitTreeSource(command_timeout_seconds=10),
    )
    request = WorkspaceOverlayRequest.model_validate(
        {
            "project_id": "h5vision/index-example",
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
            canonical_name="h5vision/index-example",
            display_name="Index Example",
            provider="git",
            remote_url=str(repository_path),
            default_branch_ref="refs/heads/main",
        )
        session.add(repository)
        session.flush()
        session.add(
            BranchBinding(
                binding_id=binding_id,
                frontend_project_id="h5vision/index-example",
                frontend_workspace_name="index-example",
                repository_id=repository.repository_id,
                branch_ref="refs/heads/main",
                vss_project_id="index-example--main",
                active=True,
            )
        )
        session.flush()
        session.add(
            Snapshot(
                snapshot_id=snapshot_id,
                request_id=uuid4(),
                binding_id=binding_id,
                frontend_project_id="h5vision/index-example",
                repository_id=repository.repository_id,
                branch_ref="refs/heads/main",
                vss_project_id="index-example--main",
                base_revision=base_revision,
                target_revision=target_revision,
                source_type="remote_clone",
                state="materialized",
                attempt_count=0,
                materialized_locator=materialized.locator,
            )
        )
        session.commit()
    sync_engine.dispose()
    return database_path, str(snapshot_id), target_revision


def build_service(
    tmp_path: Path,
    database_path: Path,
    transport: httpx2.BaseTransport,
) -> tuple[SnapshotIndexService, VssHttpClient, object]:
    engine = create_engine_from_url(f"sqlite+aiosqlite:///{database_path}")
    client = VssHttpClient(
        base_url="http://vss.example:8200",
        transport=transport,
    )
    service = SnapshotIndexService(
        sessionmaker=create_sessionmaker(engine),
        materializer=SnapshotMaterializer(
            root=tmp_path / "snapshots",
            source=GitTreeSource(command_timeout_seconds=10),
        ),
        vss_client=client,
    )
    return service, client, engine


def test_index_materialized_snapshot_submits_exact_tree_and_creates_first_attempt(
    tmp_path: Path,
) -> None:
    database_path, snapshot_id, target_revision = prepare_materialized_snapshot(tmp_path)
    submitted: list[dict] = []
    seen: list[str] = []

    def fake_vss(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        if request.url.path == "/index/status":
            return httpx2.Response(
                200,
                json={"project_id": "index-example--main", "state": "none"},
            )
        if request.url.path == "/index/exists":
            return httpx2.Response(
                200,
                json={"project_id": "index-example--main", "exists": False},
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
                    "project_id": "index-example--main",
                    "state": "running",
                },
            )
        raise AssertionError(f"unexpected VSS path: {request.url.path}")

    async def scenario() -> None:
        service, client, engine = build_service(
            tmp_path,
            database_path,
            httpx2.MockTransport(fake_vss),
        )
        try:
            outcome = await service.index(UUID(snapshot_id), request_id=uuid4())
            assert outcome.status_code == 202
            assert outcome.body.reason == "VSS_INDEX_ACCEPTED"
            assert outcome.body.state == "accepted"
            assert outcome.body.attempt_count == 1
        finally:
            client.close()
            await engine.dispose()

    asyncio.run(scenario())

    assert seen == ["/index/status", "/index/exists", "/index"]
    assert len(submitted) == 1
    assert submitted[0]["project_id"] == "index-example--main"
    assert submitted[0]["force"] is False
    assert submitted[0]["briefing"] is True
    assert "remote" not in submitted[0]
    assert "revision" not in submitted[0]
    assert "snapshot_id" not in submitted[0]

    engine = create_engine(
        f"sqlite:///{database_path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    with Session(engine) as session:
        snapshot = session.scalar(select(Snapshot))
        assert snapshot is not None
        assert snapshot.state == "accepted"
        assert snapshot.attempt_count == 1
        assert session.scalar(select(func.count()).select_from(SnapshotAttempt)) == 1
    engine.dispose()


def test_index_is_idempotent_when_exact_target_is_already_active(tmp_path: Path) -> None:
    database_path, snapshot_id, target_revision = prepare_materialized_snapshot(tmp_path)
    seen: list[str] = []

    def fake_vss(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        if request.url.path == "/index/status":
            return httpx2.Response(
                200,
                json={"project_id": "index-example--main", "state": "none"},
            )
        if request.url.path == "/index/exists":
            return httpx2.Response(
                200,
                json={
                    "project_id": "index-example--main",
                    "exists": True,
                    "chunks": 9,
                    "commit": target_revision,
                },
            )
        raise AssertionError(f"unexpected VSS path: {request.url.path}")

    async def scenario() -> None:
        service, client, engine = build_service(
            tmp_path,
            database_path,
            httpx2.MockTransport(fake_vss),
        )
        try:
            outcome = await service.index(UUID(snapshot_id), request_id=uuid4())
            assert outcome.status_code == 200
            assert outcome.body.reason == "TARGET_ALREADY_INDEXED"
            assert outcome.body.state == "already_indexed"
            assert outcome.body.attempt_count == 0
        finally:
            client.close()
            await engine.dispose()

    asyncio.run(scenario())
    assert seen == ["/index/status", "/index/exists"]


def test_index_is_blocked_while_vss_job_is_running_without_attempt(tmp_path: Path) -> None:
    database_path, snapshot_id, _ = prepare_materialized_snapshot(tmp_path)
    seen: list[str] = []

    def fake_vss(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        return httpx2.Response(
            200,
            json={"project_id": "index-example--main", "state": "running"},
        )

    async def scenario() -> None:
        service, client, engine = build_service(
            tmp_path,
            database_path,
            httpx2.MockTransport(fake_vss),
        )
        try:
            with pytest.raises(ApiError) as captured:
                await service.index(UUID(snapshot_id), request_id=uuid4())
            assert captured.value.status_code == 409
            assert captured.value.reason == "VSS_INDEX_ALREADY_RUNNING"
        finally:
            client.close()
            await engine.dispose()

    asyncio.run(scenario())
    assert seen == ["/index/status"]

    engine = create_engine(
        f"sqlite:///{database_path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    with Session(engine) as session:
        snapshot = session.scalar(select(Snapshot))
        assert snapshot is not None and snapshot.attempt_count == 0
        assert session.scalar(select(func.count()).select_from(SnapshotAttempt)) == 0
    engine.dispose()


def test_index_rejects_tampered_materialized_tree_before_vss_call(tmp_path: Path) -> None:
    database_path, snapshot_id, _ = prepare_materialized_snapshot(tmp_path)
    engine = create_engine(
        f"sqlite:///{database_path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    with Session(engine) as session:
        snapshot = session.scalar(select(Snapshot))
        assert snapshot is not None and snapshot.materialized_locator is not None
        materialized_root = tmp_path / "snapshots" / snapshot.materialized_locator
    engine.dispose()
    (materialized_root / "app.py").write_text("TAMPERED = True\n", encoding="utf-8")

    def must_not_call_vss(_: httpx2.Request) -> httpx2.Response:
        raise AssertionError("변조된 materialized tree는 VSS 호출 전에 차단되어야 합니다.")

    async def scenario() -> None:
        service, client, async_engine = build_service(
            tmp_path,
            database_path,
            httpx2.MockTransport(must_not_call_vss),
        )
        try:
            with pytest.raises(ApiError) as captured:
                await service.index(UUID(snapshot_id), request_id=uuid4())
            assert captured.value.reason == "SNAPSHOT_REVISION_MISMATCH"
        finally:
            client.close()
            await async_engine.dispose()

    asyncio.run(scenario())


def test_index_race_rejection_stays_retryable_instead_of_claiming_indexing(
    tmp_path: Path,
) -> None:
    database_path, snapshot_id, _ = prepare_materialized_snapshot(tmp_path)
    seen: list[str] = []

    def fake_vss(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        if request.url.path == "/index/status":
            return httpx2.Response(
                200,
                json={"project_id": "index-example--main", "state": "none"},
            )
        if request.url.path == "/index/exists":
            return httpx2.Response(
                200,
                json={"project_id": "index-example--main", "exists": False},
            )
        if request.url.path == "/index":
            return httpx2.Response(
                409,
                json={
                    "accepted": False,
                    "project_id": "index-example--main",
                    "state": "running",
                    "reason": "already_running",
                },
            )
        raise AssertionError(f"unexpected VSS path: {request.url.path}")

    async def scenario() -> None:
        service, client, async_engine = build_service(
            tmp_path,
            database_path,
            httpx2.MockTransport(fake_vss),
        )
        try:
            with pytest.raises(ApiError) as captured:
                await service.index(UUID(snapshot_id), request_id=uuid4())
            assert captured.value.status_code == 409
            assert captured.value.reason == "VSS_INDEX_ALREADY_RUNNING"
        finally:
            client.close()
            await async_engine.dispose()

    asyncio.run(scenario())
    assert seen == ["/index/status", "/index/exists", "/index"]

    engine = create_engine(
        f"sqlite:///{database_path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    with Session(engine) as session:
        snapshot = session.scalar(select(Snapshot))
        attempt = session.scalar(select(SnapshotAttempt))
        assert snapshot is not None
        assert snapshot.state == "rejected"
        assert snapshot.attempt_count == 1
        assert attempt is not None
        assert attempt.retryable is True
        assert attempt.vss_reason == "already_running"
    engine.dispose()
