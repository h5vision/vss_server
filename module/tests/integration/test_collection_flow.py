"""Integration test for Repository & Branch collection flow."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from uuid import uuid4

import httpx2
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.app import create_app
from backend.core.config import Settings
from backend.infrastructure.database.base import Base
from backend.infrastructure.database.models import (
    Repository,
    RepositorySyncRun,
    Snapshot,
    SnapshotAttempt,
)


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
    git(path, "config", "user.email", "collector@example.com")
    git(path, "config", "user.name", "Collector Test")
    (path / "app.py").write_text("print('version 1')\n", "utf-8")
    git(path, "add", "--all")
    git(path, "commit", "-m", "v1")
    c1 = git(path, "rev-parse", "HEAD")

    git(path, "checkout", "-b", "dev")
    (path / "app.py").write_text("print('version 2')\n", "utf-8")
    git(path, "add", "--all")
    git(path, "commit", "-m", "v2")
    c2 = git(path, "rev-parse", "HEAD")

    git(path, "checkout", "main")
    return c1, c2


def database_engine(database_path: Path):
    return create_engine(
        f"sqlite:///{database_path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )


def test_collection_end_to_end_flow(tmp_path: Path) -> None:
    # 1. Prepare source repository
    source_dir = tmp_path / "source_repo"
    source_dir.mkdir()
    c1, c2 = init_source_repo(source_dir)

    # 2. Prepare DB
    db_file = tmp_path / "app.sqlite"
    engine = database_engine(db_file)
    Base.metadata.create_all(engine)

    repo_id = uuid4()
    with Session(engine) as session:
        repo = Repository(
            repository_id=repo_id,
            canonical_name="h5vision/sample",
            display_name="Sample Project",
            provider="github",
            remote_url=str(source_dir.resolve()),
            default_branch_ref="refs/heads/main",
            active=True,
        )
        session.add(repo)
        session.commit()

    # 3. Setup mock VSS Transport
    vss_calls: list[dict] = []

    def vss_handler(request: httpx2.Request) -> httpx2.Response:
        url_str = str(request.url)
        if request.method == "POST" and url_str.endswith("/index"):
            body = json.loads(request.content.decode("utf-8"))
            vss_calls.append(body)
            return httpx2.Response(
                202,
                json={
                    "accepted": True,
                    "project_id": body.get("project_id", "prj_sample_main"),
                    "state": "running",
                },
            )
        if request.method == "GET" and "/health" in url_str:
            return httpx2.Response(200, json={"status": "ok"})
        if request.method == "GET" and "/projects" in url_str:
            return httpx2.Response(200, json=["prj_vss_1"])
        return httpx2.Response(404, json={"detail": "not found"})

    transport = httpx2.MockTransport(vss_handler)

    settings = Settings(
        database_url=SecretStr(f"sqlite+aiosqlite:///{db_file}"),
        snapshot_materialization_root=tmp_path / "snapshots",
        snapshot_collection_root=tmp_path / "repos",
        snapshot_vss_api_token=SecretStr("test-secret-token"),
        snapshot_recovery_on_startup=False,
    )

    app = create_app(settings, vss_transport=transport)

    with TestClient(app) as client:
        auth_headers = {"X-Snapshot-Token": "test-secret-token"}

        # 4. Auth check
        resp_unauth = client.get(f"/v1/internal/collection/repositories/{repo_id}/catalog")
        assert resp_unauth.status_code == 401

        # 5. Catalog check
        resp_catalog = client.get(
            f"/v1/internal/collection/repositories/{repo_id}/catalog",
            headers=auth_headers,
        )
        assert resp_catalog.status_code == 200
        catalog_data = resp_catalog.json()
        assert len(catalog_data["branches"]) == 2
        refs = {b["branch_ref"]: b["head_sha"] for b in catalog_data["branches"]}
        assert refs["refs/heads/main"] == c1
        assert refs["refs/heads/dev"] == c2

        # 6. Track branch
        resp_track = client.post(
            f"/v1/internal/collection/repositories/{repo_id}/branches",
            headers=auth_headers,
            json={
                "branch_ref": "refs/heads/main",
                "vss_project_id": "prj_sample_main",
            },
        )
        assert resp_track.status_code == 201
        tracked_info = resp_track.json()
        tracked_id = tracked_info["tracked_branch_id"]
        assert tracked_info["branch_ref"] == "refs/heads/main"
        assert tracked_info["tracked"] is True

        # 7. Trigger Sync
        resp_sync = client.post(
            f"/v1/internal/collection/repositories/{repo_id}/sync",
            headers=auth_headers,
            json={"trigger": "manual"},
        )
        assert resp_sync.status_code == 200
        sync_data = resp_sync.json()
        assert sync_data["ok"] is True
        assert sync_data["state"] == "succeeded"
        assert sync_data["observed_branches"] == 2
        assert sync_data["changed_branches"] == 2
        assert sync_data["snapshots_created"] == 2
        assert sync_data["snapshots_accepted"] == 2

        # VSS index calls verified for all auto-discovered branches
        assert len(vss_calls) == 2
        assert {c["project_id"] for c in vss_calls} == {"prj_sample_main", "h5vision/sample-dev"}

        # 8. Check History
        resp_hist = client.get(
            f"/v1/internal/collection/tracked-branches/{tracked_id}/history",
            headers=auth_headers,
        )
        assert resp_hist.status_code == 200
        hist_data = resp_hist.json()
        assert len(hist_data["history"]) == 1
        assert hist_data["history"][0]["observed_head_sha"] == c1
        assert hist_data["history"][0]["change_type"] == "initial"

        # 9. Second Sync (Idempotency - No change)
        resp_sync2 = client.post(
            f"/v1/internal/collection/repositories/{repo_id}/sync",
            headers=auth_headers,
            json={"trigger": "manual"},
        )
        assert resp_sync2.status_code == 200
        sync_data2 = resp_sync2.json()
        assert sync_data2["ok"] is True
        assert sync_data2["changed_branches"] == 0
        assert sync_data2["snapshots_created"] == 0
        # No extra VSS call (idempotent, total remained 2)
        assert len(vss_calls) == 2

        # 10. Untrack branch
        resp_untrack = client.delete(
            f"/v1/internal/collection/tracked-branches/{tracked_id}",
            headers=auth_headers,
        )
        assert resp_untrack.status_code == 200
        assert resp_untrack.json()["tracked"] is False

    # 11. Verify DB state directly
    with Session(engine) as session:
        snapshots = list(session.scalars(select(Snapshot)))
        assert len(snapshots) == 2
        revisions = {s.target_revision for s in snapshots}
        assert c1 in revisions
        assert c2 in revisions

        attempts = list(session.scalars(select(SnapshotAttempt)))
        assert len(attempts) == 2
        assert all(a.upstream_status_code == 202 for a in attempts)

        runs = list(session.scalars(select(RepositorySyncRun)))
        assert len(runs) == 2
        assert all(r.state == "succeeded" for r in runs)
