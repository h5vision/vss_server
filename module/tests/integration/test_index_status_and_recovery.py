"""Snapshot/VSS 상태 동기화와 재시작 복구 통합을 검증한다."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.app import create_app
from backend.bootstrap.container import build_container
from backend.core.config import Settings
from backend.features.indexing.recovery import SnapshotRecoveryCoordinator
from backend.infrastructure.database.base import Base
from backend.infrastructure.database.engine import create_engine_from_url, create_sessionmaker
from backend.infrastructure.database.models import BranchBinding, Repository, Snapshot
from backend.integrations.vss.client import VssHttpClient

TARGET = "2" * 40


def sync_engine(database_path: Path):
    return create_engine(
        f"sqlite:///{database_path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )


def seed_snapshot(database_path: Path, *, state: str = "accepted") -> str:
    engine = sync_engine(database_path)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        repository = Repository(
            repository_id=uuid4(),
            canonical_name="h5vision/vision",
            display_name="Vision",
            provider="github",
            remote_url="https://github.com/h5vision/vision.git",
            default_branch_ref="refs/heads/frontend",
        )
        session.add(repository)
        session.flush()
        binding = BranchBinding(
            frontend_project_id="h5vision/vision",
            frontend_workspace_name="vision",
            repository_id=repository.repository_id,
            branch_ref="refs/heads/frontend",
            vss_project_id="vision--frontend",
            active=True,
        )
        session.add(binding)
        session.flush()
        snapshot = Snapshot(
            request_id=uuid4(),
            binding_id=binding.binding_id,
            frontend_project_id="h5vision/vision",
            repository_id=repository.repository_id,
            branch_ref=binding.branch_ref,
            vss_project_id=binding.vss_project_id,
            base_revision="1" * 40,
            target_revision=TARGET,
            source_type="remote_clone",
            materialized_locator="/srv/private/vision",
            state=state,
            attempt_count=1,
        )
        session.add(snapshot)
        session.commit()
        snapshot_id = str(snapshot.snapshot_id)
    engine.dispose()
    return snapshot_id


def done_status(commit: str = TARGET, *, index_id: str = "vision--frontend") -> dict:
    return {
        "project_id": "vision--frontend",
        "index_id": index_id,
        "state": "done",
        "processed": 5,
        "total": 5,
        "chunk_count": 12,
        "error": None,
        "mode": "incremental",
        "briefing": "ready",
        "briefing_error": None,
        "elapsed_s": 4.2,
        "index": {
            "commit": commit,
            "chunks": 12,
            "dirty": False,
            "project_root": "/srv/private/vision",
            "bm25_count": 12,
            "mode": "incremental",
            "incremental": {
                "changed_files": 2,
                "deleted_files": 1,
                "unchanged_files": 7,
                "reused_chunks": 30,
                "rebuilt_chunks": 5,
            },
        },
        "incomplete": [{"path": "/srv/private/incomplete"}],
    }


def test_frontend_status_marks_only_exact_done_revision_completed(tmp_path: Path) -> None:
    database_path = tmp_path / "snapshot.db"
    seed_snapshot(database_path)

    def fake_vss(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/index/status"
        assert request.url.params["project_id"] == "vision--frontend"
        return httpx2.Response(200, json=done_status())

    app = create_app(
        Settings(
            vision_environment="test",
            database_url=f"sqlite+aiosqlite:///{database_path}",
            snapshot_recovery_on_startup=False,
            docs_enabled=False,
        ),
        vss_transport=httpx2.MockTransport(fake_vss),
    )

    with TestClient(app) as client:
        response = client.get("/v1/index/status", params={"project_id": "vision"})

    assert response.status_code == 200
    assert response.json()["reason"] == "VSS_INDEX_COMPLETED"
    assert response.json()["state"] == "completed"
    assert response.json()["target_revision"] == TARGET
    assert response.json()["vss"] == {
        "state": "done",
        "index_id": "vision--frontend",
        "index_matches_project": True,
        "mode": "incremental",
        "processed": 5,
        "total": 5,
        "chunk_count": 12,
        "active_revision": TARGET,
        "revision_matches": True,
        "source_matches_snapshot": True,
        "dirty": False,
        "bm25_count": 12,
        "incremental": {
            "changed_files": 2,
            "deleted_files": 1,
            "unchanged_files": 7,
            "reused_chunks": 30,
            "rebuilt_chunks": 5,
        },
        "briefing": "ready",
        "briefing_error": None,
        "elapsed_s": 4.2,
    }
    assert "/srv/private" not in response.text

    engine = sync_engine(database_path)
    with Session(engine) as session:
        snapshot = session.scalar(select(Snapshot))
        assert snapshot is not None
        assert snapshot.state == "completed"
        assert snapshot.vss_reason == "VSS_INDEX_COMPLETED"
    engine.dispose()



def test_status_does_not_adopt_a_resolved_sibling_index(tmp_path: Path) -> None:
    database_path = tmp_path / "snapshot-sibling.db"
    seed_snapshot(database_path)

    def fake_vss(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/index/status":
            return httpx2.Response(
                200,
                json=done_status(index_id="vision--frontend--ast-v3-abcdef0"),
            )
        if request.url.path == "/index/exists":
            return httpx2.Response(
                200,
                json={
                    "project_id": "vision--frontend",
                    "index_id": "vision--frontend--ast-v3-abcdef0",
                    "exists": True,
                    "chunks": 12,
                    "commit": TARGET,
                },
            )
        return httpx2.Response(404, json={"detail": "not found"})

    app = create_app(
        Settings(
            vision_environment="test",
            database_url=f"sqlite+aiosqlite:///{database_path}",
            snapshot_recovery_on_startup=False,
            docs_enabled=False,
        ),
        vss_transport=httpx2.MockTransport(fake_vss),
    )

    with TestClient(app) as client:
        response = client.get("/v1/index/status", params={"project_id": "vision"})

    assert response.status_code == 200
    assert response.json()["reason"] == "VSS_INDEX_ID_MISMATCH"
    assert response.json()["state"] == "indexing"
    assert response.json()["vss"]["index_id"] == "vision--frontend--ast-v3-abcdef0"
    assert response.json()["vss"]["index_matches_project"] is False

    engine = sync_engine(database_path)
    with Session(engine) as session:
        snapshot = session.scalar(select(Snapshot))
        assert snapshot is not None
        assert snapshot.state == "indexing"
        assert snapshot.vss_reason == "VSS_INDEX_ID_MISMATCH"
    engine.dispose()

def test_done_with_another_commit_is_a_non_retryable_revision_mismatch(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "snapshot.db"
    seed_snapshot(database_path)

    def mismatch(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=done_status("3" * 40))

    app = create_app(
        Settings(
            vision_environment="test",
            database_url=f"sqlite+aiosqlite:///{database_path}",
            snapshot_recovery_on_startup=False,
            docs_enabled=False,
        ),
        vss_transport=httpx2.MockTransport(mismatch),
    )

    with TestClient(app) as client:
        response = client.get("/v1/index/status", params={"project_id": "h5vision/vision"})

    assert response.status_code == 200
    assert response.json()["reason"] == "VSS_REVISION_MISMATCH"
    assert response.json()["state"] == "failed"
    assert response.json()["retryable"] is False


def test_none_status_uses_exact_active_index_commit_as_recovery_evidence(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "snapshot.db"
    seed_snapshot(database_path)
    seen: list[str] = []

    def fake_vss(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        if request.url.path == "/index/status":
            return httpx2.Response(
                200,
                json={"project_id": "vision--frontend", "state": "none"},
            )
        if request.url.path == "/index/exists":
            return httpx2.Response(
                200,
                json={
                    "project_id": "vision--frontend",
                    "exists": True,
                    "chunks": 12,
                    "commit": TARGET,
                },
            )
        raise AssertionError(f"unexpected VSS path: {request.url.path}")

    app = create_app(
        Settings(
            vision_environment="test",
            database_url=f"sqlite+aiosqlite:///{database_path}",
            snapshot_recovery_on_startup=False,
            docs_enabled=False,
        ),
        vss_transport=httpx2.MockTransport(fake_vss),
    )

    with TestClient(app) as client:
        response = client.get("/v1/index/status", params={"project_id": "vision"})

    assert response.status_code == 200
    assert response.json()["reason"] == "TARGET_ALREADY_INDEXED"
    assert response.json()["state"] == "completed"
    assert seen == ["/index/status", "/index/exists"]


def test_restart_recovery_synchronizes_non_terminal_snapshots_once(tmp_path: Path) -> None:
    database_path = tmp_path / "snapshot.db"
    seed_snapshot(database_path, state="accepted")

    def fake_vss(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/index/status"
        return httpx2.Response(200, json=done_status())

    async def scenario() -> None:
        engine = create_engine_from_url(f"sqlite+aiosqlite:///{database_path}")
        sessionmaker = create_sessionmaker(engine)
        client = VssHttpClient(
            base_url="http://vss.example:8200",
            transport=httpx2.MockTransport(fake_vss),
        )
        summary = await SnapshotRecoveryCoordinator(
            engine=engine,
            sessionmaker=sessionmaker,
            vss_client=client,
        ).run_once()
        client.close()
        await engine.dispose()
        assert summary.model_dump() == {
            "lock_acquired": True,
            "examined": 1,
            "synchronized": 1,
            "unavailable": 0,
            "failed": 0,
        }

    asyncio.run(scenario())

    engine = sync_engine(database_path)
    with Session(engine) as session:
        snapshot = session.scalar(select(Snapshot))
        assert snapshot is not None
        assert snapshot.state == "completed"
    engine.dispose()


def test_periodic_reconciler_converges_accepted_snapshot_without_status_request(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "snapshot.db"
    seed_snapshot(database_path, state="accepted")
    seen: list[str] = []

    def fake_vss(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        assert request.url.path == "/index/status"
        return httpx2.Response(200, json=done_status())

    async def scenario() -> None:
        container = build_container(
            Settings(
                vision_environment="test",
                database_url=f"sqlite+aiosqlite:///{database_path}",
                snapshot_recovery_on_startup=False,
                snapshot_reconcile_enabled=True,
                snapshot_reconcile_interval_seconds=0.01,
                snapshot_reconcile_batch_size=10,
                docs_enabled=False,
            ),
            vss_transport=httpx2.MockTransport(fake_vss),
            start_recovery=True,
        )
        assert container.snapshot_recovery_task is None
        assert container.snapshot_reconciler_task is not None
        try:
            completed = False
            for _ in range(50):
                await asyncio.sleep(0.01)
                assert container.db_sessionmaker is not None
                async with container.db_sessionmaker() as session:
                    snapshot = await session.scalar(select(Snapshot))
                    if snapshot is not None and snapshot.state == "completed":
                        completed = True
                        break
            assert completed is True
        finally:
            await container.dispose()

    asyncio.run(scenario())
    assert seen == ["/index/status"]


@pytest.mark.parametrize(
    ("vss_state", "snapshot_state", "reason", "retryable"),
    [
        ("running", "indexing", "VSS_INDEX_IN_PROGRESS", False),
        ("indexing_lexical", "indexing", "VSS_INDEX_IN_PROGRESS", False),
        ("promoting", "indexing", "VSS_INDEX_IN_PROGRESS", False),
        ("failed", "failed", "VSS_INDEX_FAILED", True),
        ("aborted", "aborted", "VSS_INDEX_ABORTED", True),
    ],
)
def test_status_maps_each_vss_job_state_to_a_reason(
    tmp_path: Path,
    vss_state: str,
    snapshot_state: str,
    reason: str,
    retryable: bool,
) -> None:
    database_path = tmp_path / "snapshot.db"
    seed_snapshot(database_path)

    def fake_vss(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "project_id": "vision--frontend",
                "state": vss_state,
                "processed": 2,
                "total": 5,
                "error": "/srv/private must never be returned",
            },
        )

    app = create_app(
        Settings(
            vision_environment="test",
            database_url=f"sqlite+aiosqlite:///{database_path}",
            snapshot_recovery_on_startup=False,
            docs_enabled=False,
        ),
        vss_transport=httpx2.MockTransport(fake_vss),
    )

    with TestClient(app) as client:
        response = client.get("/v1/index/status", params={"project_id": "vision"})

    assert response.status_code == 200
    assert response.json()["state"] == snapshot_state
    assert response.json()["reason"] == reason
    assert response.json()["retryable"] is retryable
    assert "/srv/private" not in response.text


def test_status_transport_failure_is_safe_and_retryable(tmp_path: Path) -> None:
    database_path = tmp_path / "snapshot.db"
    seed_snapshot(database_path)

    def unavailable(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("secret upstream address", request=request)

    app = create_app(
        Settings(
            vision_environment="test",
            database_url=f"sqlite+aiosqlite:///{database_path}",
            snapshot_recovery_on_startup=False,
            docs_enabled=False,
        ),
        vss_transport=httpx2.MockTransport(unavailable),
    )

    with TestClient(app) as client:
        response = client.get("/v1/index/status", params={"project_id": "vision"})

    assert response.status_code == 503
    assert response.json()["reason"] == "VSS_HTTP_UNAVAILABLE"
    assert response.json()["retryable"] is True
    assert "secret upstream address" not in response.text


def test_restart_recovery_counts_unavailable_vss_without_resubmitting(tmp_path: Path) -> None:
    database_path = tmp_path / "snapshot.db"
    seed_snapshot(database_path, state="accepted")
    seen: list[str] = []

    def unavailable(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        raise httpx2.ConnectError("VSS unavailable", request=request)

    async def scenario() -> None:
        engine = create_engine_from_url(f"sqlite+aiosqlite:///{database_path}")
        sessionmaker = create_sessionmaker(engine)
        client = VssHttpClient(
            base_url="http://vss.example:8200",
            transport=httpx2.MockTransport(unavailable),
        )
        summary = await SnapshotRecoveryCoordinator(
            engine=engine,
            sessionmaker=sessionmaker,
            vss_client=client,
        ).run_once()
        client.close()
        await engine.dispose()
        assert summary.model_dump() == {
            "lock_acquired": True,
            "examined": 1,
            "synchronized": 0,
            "unavailable": 1,
            "failed": 0,
        }

    asyncio.run(scenario())
    assert seen == ["/index/status"]

    engine = sync_engine(database_path)
    with Session(engine) as session:
        snapshot = session.scalar(select(Snapshot))
        assert snapshot is not None
        assert snapshot.state == "accepted"
        assert snapshot.attempt_count == 1
    engine.dispose()
