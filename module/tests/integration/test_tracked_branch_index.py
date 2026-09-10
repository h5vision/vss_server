"""Tracked Branch Index always consumes a verified immutable Snapshot tree."""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import httpx2
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.core.errors import ApiError
from backend.features.indexing.index import SnapshotIndexService
from backend.features.materialization.service import SnapshotMaterializer
from backend.features.materialization.source import GitTreeSource
from backend.features.workspace_overlays.schemas import WorkspaceOverlayRequest
from backend.infrastructure.database.base import Base
from backend.infrastructure.database.engine import create_engine_from_url, create_sessionmaker
from backend.infrastructure.database.models import Repository, Snapshot, TrackedBranch
from backend.integrations.vss.client import VssHttpClient


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


@dataclass(frozen=True, slots=True)
class Fixture:
    database_path: Path
    remote: Path
    source: Path
    repository_id: UUID
    tracked_branch_id: UUID
    snapshot_id: UUID
    base_revision: str
    target_revision: str


def _materialize(
    tmp_path: Path,
    *,
    remote: Path,
    branch_ref: str,
    snapshot_id: UUID,
    base_revision: str,
    target_revision: str,
    content: str,
):
    materializer = SnapshotMaterializer(
        root=tmp_path / "snapshots",
        source=GitTreeSource(command_timeout_seconds=10),
    )
    return materializer.materialize(
        WorkspaceOverlayRequest.model_validate(
            {
                "project_id": "h5vision/index-example",
                "base_revision": base_revision,
                "target_revision": target_revision,
                "files": [
                    {
                        "status": "modified",
                        "path": "app.py",
                        "content": content,
                        "encoding": "utf-8",
                    }
                ],
                "deleted_paths": [],
                "renames": [],
            }
        ),
        binding_id=uuid4(),
        snapshot_id=snapshot_id,
        remote_url=str(remote),
        branch_ref=branch_ref,
    )


def prepare_fixture(tmp_path: Path) -> Fixture:
    remote = tmp_path / "index-example.git"
    source = tmp_path / "source"
    remote.mkdir()
    source.mkdir()
    git(remote, "init", "--bare")
    git(source, "init", "-b", "main")
    git(source, "config", "user.email", "branch-index@example.invalid")
    git(source, "config", "user.name", "Branch Index Test")
    git(source, "remote", "add", "origin", str(remote))
    (source / "app.py").write_text("VERSION = 1\n", encoding="utf-8")
    git(source, "add", "--all")
    git(source, "commit", "-m", "base")
    base_revision = git(source, "rev-parse", "HEAD")
    git(source, "push", "-u", "origin", "main")

    git(source, "checkout", "-b", "feature/login")
    (source / "app.py").write_text("VERSION = 2\n", encoding="utf-8")
    git(source, "add", "--all")
    git(source, "commit", "-m", "target")
    target_revision = git(source, "rev-parse", "HEAD")
    git(source, "push", "-u", "origin", "feature/login")

    database_path = tmp_path / "tracked-index.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    Base.metadata.create_all(engine)
    repository_id = uuid4()
    tracked_branch_id = uuid4()
    snapshot_id = uuid4()
    materialized = _materialize(
        tmp_path,
        remote=remote,
        branch_ref="refs/heads/feature/login",
        snapshot_id=snapshot_id,
        base_revision=base_revision,
        target_revision=target_revision,
        content="VERSION = 2\n",
    )
    with Session(engine) as session:
        session.add(
            Repository(
                repository_id=repository_id,
                canonical_name="h5vision/index-example.git",
                display_name="Index Example",
                provider="git",
                remote_url=str(remote),
                default_branch_ref="refs/heads/main",
            )
        )
        session.add(
            TrackedBranch(
                tracked_branch_id=tracked_branch_id,
                repository_id=repository_id,
                branch_ref="refs/heads/feature/login",
                vss_project_id="index-example--feature-login",
                tracked=True,
                current_head_sha=target_revision,
            )
        )
        session.add(
            Snapshot(
                snapshot_id=snapshot_id,
                request_id=uuid4(),
                tracked_branch_id=tracked_branch_id,
                frontend_project_id=None,
                repository_id=repository_id,
                branch_ref="refs/heads/feature/login",
                vss_project_id="index-example--feature-login",
                base_revision=base_revision,
                target_revision=target_revision,
                source_type="remote_clone",
                state="materialized",
                attempt_count=0,
                materialized_locator=materialized.locator,
            )
        )
        session.commit()
    engine.dispose()
    return Fixture(
        database_path=database_path,
        remote=remote,
        source=source,
        repository_id=repository_id,
        tracked_branch_id=tracked_branch_id,
        snapshot_id=snapshot_id,
        base_revision=base_revision,
        target_revision=target_revision,
    )


def build_service(tmp_path: Path, fixture: Fixture, transport: httpx2.BaseTransport):
    engine = create_engine_from_url(f"sqlite+aiosqlite:///{fixture.database_path}")
    client = VssHttpClient(base_url="http://vss.example:8200", transport=transport)
    service = SnapshotIndexService(
        sessionmaker=create_sessionmaker(engine),
        materializer=SnapshotMaterializer(
            root=tmp_path / "snapshots",
            source=GitTreeSource(command_timeout_seconds=10),
        ),
        vss_client=client,
    )
    return service, client, engine


def test_tracked_branch_index_submits_immutable_snapshot_root(tmp_path: Path) -> None:
    fixture = prepare_fixture(tmp_path)
    submitted: list[dict] = []

    def fake_vss(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/index/status":
            return httpx2.Response(
                200, json={"project_id": "index-example--feature-login", "state": "none"}
            )
        if request.url.path == "/index/exists":
            return httpx2.Response(
                200, json={"project_id": "index-example--feature-login", "exists": False}
            )
        if request.url.path == "/index":
            body = json.loads(request.content)
            submitted.append(body)
            root = Path(body["project_root"])
            root.relative_to((tmp_path / "snapshots").resolve())
            assert ".snapshot-worktrees" not in root.parts
            assert git(root, "rev-parse", "HEAD") == fixture.target_revision
            assert git(root, "status", "--porcelain=v1") == ""
            return httpx2.Response(
                202,
                json={
                    "accepted": True,
                    "project_id": "index-example--feature-login",
                    "state": "running",
                },
            )
        raise AssertionError(f"unexpected VSS path: {request.url.path}")

    async def scenario() -> None:
        service, client, engine = build_service(tmp_path, fixture, httpx2.MockTransport(fake_vss))
        try:
            outcome = await service.index_tracked_branch(
                fixture.tracked_branch_id, request_id=uuid4()
            )
            assert outcome.status_code == 202
            assert outcome.body.reason == "VSS_INDEX_ACCEPTED"
        finally:
            client.close()
            await engine.dispose()

    asyncio.run(scenario())
    assert len(submitted) == 1
    assert submitted[0]["force"] is False
    assert submitted[0]["note"] == f"snapshot {fixture.target_revision}"


def test_remote_drift_does_not_change_verified_snapshot_source(tmp_path: Path) -> None:
    fixture = prepare_fixture(tmp_path)
    submitted_roots: list[Path] = []

    (fixture.source / "app.py").write_text("VERSION = 99\n", encoding="utf-8")
    git(fixture.source, "add", "--all")
    git(fixture.source, "commit", "-m", "remote drift")
    drift_revision = git(fixture.source, "rev-parse", "HEAD")
    git(fixture.source, "push", "origin", "feature/login")
    assert drift_revision != fixture.target_revision

    def fake_vss(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/index/status":
            return httpx2.Response(
                200, json={"project_id": "index-example--feature-login", "state": "none"}
            )
        if request.url.path == "/index/exists":
            return httpx2.Response(
                200, json={"project_id": "index-example--feature-login", "exists": False}
            )
        if request.url.path == "/index":
            root = Path(json.loads(request.content)["project_root"])
            submitted_roots.append(root)
            assert git(root, "rev-parse", "HEAD") == fixture.target_revision
            assert git(root, "rev-parse", "HEAD") != drift_revision
            return httpx2.Response(
                202,
                json={
                    "accepted": True,
                    "project_id": "index-example--feature-login",
                    "state": "running",
                },
            )
        raise AssertionError(f"unexpected VSS path: {request.url.path}")

    async def scenario() -> None:
        service, client, engine = build_service(tmp_path, fixture, httpx2.MockTransport(fake_vss))
        try:
            outcome = await service.index_tracked_branch(
                fixture.tracked_branch_id, request_id=uuid4()
            )
            assert outcome.status_code == 202
        finally:
            client.close()
            await engine.dispose()

    asyncio.run(scenario())
    assert len(submitted_roots) == 1


def test_required_tracked_branch_is_revalidated_in_index_transaction(tmp_path: Path) -> None:
    fixture = prepare_fixture(tmp_path)

    def must_not_call_vss(_request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("stale tracked Branch selection must fail before any VSS request")

    service, client, async_engine = build_service(
        tmp_path, fixture, httpx2.MockTransport(must_not_call_vss)
    )
    sync_engine = create_engine(
        f"sqlite:///{fixture.database_path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    with Session(sync_engine) as session:
        tracked_branch = session.get(TrackedBranch, fixture.tracked_branch_id)
        assert tracked_branch is not None
        tracked_branch.current_head_sha = "f" * 40
        session.commit()
    sync_engine.dispose()

    async def scenario() -> None:
        try:
            with pytest.raises(ApiError) as captured:
                await service.index(
                    fixture.snapshot_id,
                    request_id=uuid4(),
                    required_tracked_branch_id=fixture.tracked_branch_id,
                )
            assert captured.value.reason == "TRACKED_BRANCH_SNAPSHOT_REQUIRED"
        finally:
            client.close()
            await async_engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("active_state", ["submitting", "accepted", "indexing"])
def test_active_snapshot_blocks_new_head_before_vss(tmp_path: Path, active_state: str) -> None:
    fixture = prepare_fixture(tmp_path)

    def must_not_call_vss(_request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("DB submitting guard must fail before any VSS request")

    service, client, async_engine = build_service(
        tmp_path, fixture, httpx2.MockTransport(must_not_call_vss)
    )
    (fixture.source / "app.py").write_text("VERSION = 4\n", encoding="utf-8")
    git(fixture.source, "add", "--all")
    git(fixture.source, "commit", "-m", "next head")
    next_revision = git(fixture.source, "rev-parse", "HEAD")
    git(fixture.source, "push", "origin", "feature/login")
    next_snapshot_id = uuid4()
    materialized = _materialize(
        tmp_path,
        remote=fixture.remote,
        branch_ref="refs/heads/feature/login",
        snapshot_id=next_snapshot_id,
        base_revision=fixture.target_revision,
        target_revision=next_revision,
        content="VERSION = 4\n",
    )
    sync_engine = create_engine(
        f"sqlite:///{fixture.database_path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    with Session(sync_engine) as session:
        previous = session.get(Snapshot, fixture.snapshot_id)
        tracked_branch = session.get(TrackedBranch, fixture.tracked_branch_id)
        assert previous is not None and tracked_branch is not None
        previous.state = active_state
        tracked_branch.current_head_sha = next_revision
        session.add(
            Snapshot(
                snapshot_id=next_snapshot_id,
                request_id=uuid4(),
                tracked_branch_id=fixture.tracked_branch_id,
                frontend_project_id=None,
                repository_id=fixture.repository_id,
                branch_ref="refs/heads/feature/login",
                vss_project_id="index-example--feature-login",
                base_revision=fixture.target_revision,
                target_revision=next_revision,
                source_type="remote_clone",
                state="materialized",
                attempt_count=0,
                materialized_locator=materialized.locator,
            )
        )
        session.commit()
    sync_engine.dispose()

    async def scenario() -> None:
        try:
            with pytest.raises(ApiError) as captured:
                await service.index_tracked_branch(fixture.tracked_branch_id, request_id=uuid4())
            assert captured.value.reason == "VSS_INDEX_ALREADY_RUNNING"
        finally:
            client.close()
            await async_engine.dispose()

    asyncio.run(scenario())


def test_running_vss_job_leaves_new_snapshot_materialized(tmp_path: Path) -> None:
    fixture = prepare_fixture(tmp_path)
    service, client, async_engine = build_service(
        tmp_path,
        fixture,
        httpx2.MockTransport(
            lambda _request: httpx2.Response(
                200,
                json={"project_id": "index-example--feature-login", "state": "running"},
            )
        ),
    )

    async def scenario() -> None:
        try:
            with pytest.raises(ApiError) as captured:
                await service.index_tracked_branch(fixture.tracked_branch_id, request_id=uuid4())
            assert captured.value.reason == "VSS_INDEX_ALREADY_RUNNING"
        finally:
            client.close()
            await async_engine.dispose()

    asyncio.run(scenario())

    engine = create_engine(
        f"sqlite:///{fixture.database_path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    with Session(engine) as session:
        snapshot = session.scalar(
            select(Snapshot).where(Snapshot.snapshot_id == fixture.snapshot_id)
        )
        assert snapshot is not None and snapshot.state == "materialized"
    engine.dispose()
