"""VSS pull 모드에서는 module이 인덱싱을 시작하지 않음을 검증한다."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx2
import pytest
from sqlalchemy import func, select

from backend.core.errors import ApiError
from backend.features.indexing.retry import SnapshotRetryService
from backend.features.repository_collection.publisher import CollectedSnapshotPublisher
from backend.features.workspace_overlays.schemas import (
    WorkspaceOverlayFile,
    WorkspaceOverlayRequest,
)
from backend.features.workspace_overlays.service import WorkspaceOverlayService
from backend.infrastructure.database.base import Base
from backend.infrastructure.database.engine import create_engine_from_url, create_sessionmaker
from backend.infrastructure.database.models import (
    BranchBinding,
    Repository,
    Snapshot,
    SnapshotAttempt,
    TrackedBranch,
)
from backend.integrations.vss.client import VssHttpClient


class FakeMaterializer:
    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root

    def materialize(self, *args, **kwargs):
        return SimpleNamespace(
            project_root=self._project_root,
            locator="pull/revisions/" + "2" * 40,
            source_type="remote_clone",
        )

    def verify_existing(self, *args, **kwargs):
        return SimpleNamespace(project_root=self._project_root)


def test_collector_pull_mode_materializes_without_vss_submission(tmp_path: Path) -> None:
    database_path = tmp_path / "collector-pull.db"
    vss_calls = 0

    def reject_vss_call(request: httpx2.Request) -> httpx2.Response:
        nonlocal vss_calls
        vss_calls += 1
        return httpx2.Response(500, json={"error": "module must not call VSS in pull mode"})

    async def scenario() -> None:
        engine = create_engine_from_url(f"sqlite+aiosqlite:///{database_path}")
        vss_client = VssHttpClient(
            base_url="http://vss.example:8200",
            transport=httpx2.MockTransport(reject_vss_call),
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            sessionmaker = create_sessionmaker(engine)
            async with sessionmaker() as session:
                repository = Repository(
                    canonical_name="h5vision/pull-collector",
                    display_name="Pull Collector",
                    provider="github",
                    remote_url="https://github.com/h5vision/pull-collector.git",
                    default_branch_ref="refs/heads/main",
                )
                session.add(repository)
                await session.flush()
                branch = TrackedBranch(
                    repository_id=repository.repository_id,
                    branch_ref="refs/heads/main",
                    vss_project_id="pull-collector--main",
                    current_head_sha="2" * 40,
                )
                session.add(branch)
                await session.flush()
                snapshot = Snapshot(
                    request_id=uuid4(),
                    tracked_branch_id=branch.tracked_branch_id,
                    repository_id=repository.repository_id,
                    branch_ref=branch.branch_ref,
                    vss_project_id=branch.vss_project_id,
                    base_revision="1" * 40,
                    target_revision="2" * 40,
                    state="validated",
                )
                session.add(snapshot)
                await session.commit()
                snapshot_id = snapshot.snapshot_id

            outcome = await CollectedSnapshotPublisher(
                sessionmaker=sessionmaker,
                materializer=FakeMaterializer(tmp_path / "materialized"),
                vss_client=vss_client,
                index_orchestration_mode="vss_pull",
            ).publish(snapshot_id, request_id=uuid4())

            async with sessionmaker() as session:
                persisted = await session.get(Snapshot, snapshot_id)
                attempts = await session.scalar(
                    select(func.count()).select_from(SnapshotAttempt)
                )
            assert outcome.reason == "SNAPSHOT_READY_FOR_VSS_PULL"
            assert outcome.snapshot_state == "materialized"
            assert persisted is not None and persisted.state == "materialized"
            assert persisted.attempt_count == 0
            assert attempts == 0
            assert vss_calls == 0
        finally:
            vss_client.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_workspace_overlay_pull_mode_returns_materialized_source_without_vss_call(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "overlay-pull.db"
    vss_calls = 0

    def reject_vss_call(request: httpx2.Request) -> httpx2.Response:
        nonlocal vss_calls
        vss_calls += 1
        return httpx2.Response(500, json={"error": "module must not call VSS in pull mode"})

    async def scenario() -> None:
        engine = create_engine_from_url(f"sqlite+aiosqlite:///{database_path}")
        vss_client = VssHttpClient(
            base_url="http://vss.example:8200",
            transport=httpx2.MockTransport(reject_vss_call),
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            sessionmaker = create_sessionmaker(engine)
            async with sessionmaker() as session:
                repository = Repository(
                    canonical_name="h5vision/pull-overlay",
                    display_name="Pull Overlay",
                    provider="github",
                    remote_url="https://github.com/h5vision/pull-overlay.git",
                    default_branch_ref="refs/heads/main",
                )
                session.add(repository)
                await session.flush()
                session.add(
                    BranchBinding(
                        frontend_project_id="pull-overlay",
                        repository_id=repository.repository_id,
                        branch_ref="refs/heads/main",
                        vss_project_id="pull-overlay--main",
                    )
                )
                await session.commit()

            request = WorkspaceOverlayRequest(
                project_id="pull-overlay",
                base_revision="1" * 40,
                target_revision="2" * 40,
                files=[
                    WorkspaceOverlayFile(
                        status="modified",
                        path="app.py",
                        content="VERSION = 2\n",
                        encoding="utf-8",
                    )
                ],
                deleted_paths=[],
                renames=[],
            )
            outcome = await WorkspaceOverlayService(
                sessionmaker=sessionmaker,
                materializer=FakeMaterializer(tmp_path / "materialized"),
                vss_client=vss_client,
                index_orchestration_mode="vss_pull",
            ).execute(request, request_id=uuid4())

            async with sessionmaker() as session:
                snapshot = await session.scalar(select(Snapshot))
                attempts = await session.scalar(
                    select(func.count()).select_from(SnapshotAttempt)
                )
            assert outcome.status_code == 202
            assert outcome.body.reason == "SNAPSHOT_READY_FOR_VSS_PULL"
            assert outcome.body.state == "materialized"
            assert snapshot is not None and snapshot.attempt_count == 0
            assert snapshot.source_type == "remote_clone"
            assert attempts == 0
            assert vss_calls == 0
        finally:
            vss_client.close()
            await engine.dispose()

    asyncio.run(scenario())


def test_admin_retry_is_blocked_when_vss_owns_index_start(tmp_path: Path) -> None:
    async def scenario() -> None:
        engine = create_engine_from_url(f"sqlite+aiosqlite:///{tmp_path / 'retry-pull.db'}")
        vss_client = VssHttpClient(base_url="http://vss.example:8200")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            service = SnapshotRetryService(
                sessionmaker=create_sessionmaker(engine),
                materializer=FakeMaterializer(tmp_path / "materialized"),
                vss_client=vss_client,
                index_orchestration_mode="vss_pull",
            )
            with pytest.raises(ApiError) as captured:
                await service.retry(uuid4(), request_id=uuid4())
            assert captured.value.status_code == 409
            assert captured.value.reason == "VSS_PULL_OWNS_INDEX_START"
        finally:
            vss_client.close()
            await engine.dispose()

    asyncio.run(scenario())
