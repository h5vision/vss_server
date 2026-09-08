"""VSS가 Snapshot SHA와 Git 정합성 증거를 pull하는 내부 API를 검증한다."""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
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
    ChangeRequest,
    ChangeRequestRevision,
    Repository,
    RepositoryCommit,
    RepositoryCommitParent,
    RepositoryTag,
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
    assert "token_config_path" not in response.json()
    assert "wrong-token" not in response.text


def test_vss_source_api_explains_missing_token_without_exposing_its_value(
    tmp_path: Path,
) -> None:
    config_path = "/etc/vss-snapshot/module.env"
    app = create_app(
        Settings(
            vision_environment="test",
            snapshot_materialization_root=tmp_path / "snapshots",
            snapshot_vss_api_token="shared-secret",
            snapshot_vss_api_token_config_path=config_path,
            snapshot_recovery_on_startup=False,
            docs_enabled=False,
        )
    )
    with TestClient(app) as client:
        response = client.get(
            "/v1/internal/vss/source",
            params={"project_id": "vss-server--module"},
        )

    assert response.status_code == 401
    body = response.json()
    assert body["reason"] == "VSS_SOURCE_AUTH_REQUIRED"
    assert body["token_environment_variable"] == "SNAPSHOT_VSS_API_TOKEN"
    assert body["token_config_path"] == config_path
    assert "token" in body["warning"].lower()
    assert "shared-secret" not in response.text


def test_vss_source_api_explains_where_backend_token_must_be_configured(
    tmp_path: Path,
) -> None:
    config_path = "/etc/vss-snapshot/module.env"
    app = create_app(
        Settings(
            vision_environment="test",
            snapshot_materialization_root=tmp_path / "snapshots",
            snapshot_vss_api_token=None,
            snapshot_vss_api_token_config_path=config_path,
            snapshot_recovery_on_startup=False,
            docs_enabled=False,
        )
    )
    with TestClient(app) as client:
        response = client.get(
            "/v1/internal/vss/revisions",
            params={"project_id": "vss-server--module"},
        )

    assert response.status_code == 503
    body = response.json()
    assert body["reason"] == "VSS_SOURCE_API_NOT_CONFIGURED"
    assert body["token_environment_variable"] == "SNAPSHOT_VSS_API_TOKEN"
    assert body["token_config_path"] == config_path


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


def test_vss_can_pull_change_request_context_and_revision_availability(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "change-requests.db"
    materialization_root = tmp_path / "snapshots"
    observed_at = datetime(2026, 9, 2, tzinfo=timezone.utc)
    base_sha = "1" * 40
    head_sha = "2" * 40
    merge_sha = "3" * 40
    engine = create_engine(
        f"sqlite:///{database_path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        repository = Repository(
            canonical_name="h5vision/change-context",
            display_name="Change Context",
            provider="github",
            remote_url="https://github.com/h5vision/change-context.git",
            default_branch_ref="refs/heads/main",
        )
        session.add(repository)
        session.flush()
        tracked_branch = TrackedBranch(
            repository_id=repository.repository_id,
            branch_ref="refs/heads/main",
            vss_project_id="change-context--main",
            current_head_sha=merge_sha,
        )
        session.add(tracked_branch)
        session.flush()
        change_request = ChangeRequest(
            repository_id=repository.repository_id,
            provider="github",
            external_number=42,
            kind="pull_request",
            state="merged",
            title="Add revision context",
            base_ref="refs/heads/main",
            head_ref="refs/heads/feature/context",
            current_base_sha=base_sha,
            current_head_sha=head_sha,
            current_merge_sha=merge_sha,
            last_observed_at=observed_at,
            provider_updated_at=observed_at,
            merged_at=observed_at,
        )
        session.add(change_request)
        session.flush()
        commits = {}
        for revision, subject in (
            (base_sha, "base revision"),
            (head_sha, "head revision"),
            (merge_sha, "merge revision"),
        ):
            commit = RepositoryCommit(
                repository_id=repository.repository_id,
                commit_sha=revision,
                tree_sha=(revision[0] * 40),
                author_name="Context Author",
                authored_at=observed_at,
                committed_at=observed_at,
                subject=subject,
                object_verified_at=observed_at,
                last_seen_at=observed_at,
            )
            session.add(commit)
            commits[revision] = commit
        session.flush()
        session.add(
            RepositoryCommitParent(
                repository_commit_id=commits[merge_sha].repository_commit_id,
                parent_commit_id=commits[head_sha].repository_commit_id,
                parent_sha=head_sha,
                parent_order=0,
            )
        )
        session.add(
            RepositoryTag(
                repository_id=repository.repository_id,
                tag_ref="refs/tags/v1.0.0",
                current_commit_sha=merge_sha,
                last_observed_at=observed_at,
            )
        )
        session.add_all(
            [
                ChangeRequestRevision(
                    change_request_id=change_request.change_request_id,
                    observation_key="a" * 64,
                    state="open",
                    base_ref=change_request.base_ref,
                    head_ref=change_request.head_ref,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    merge_sha=None,
                    provider_updated_at=observed_at,
                    observed_at=observed_at,
                ),
                ChangeRequestRevision(
                    change_request_id=change_request.change_request_id,
                    observation_key="b" * 64,
                    state="merged",
                    base_ref=change_request.base_ref,
                    head_ref=change_request.head_ref,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    merge_sha=merge_sha,
                    provider_updated_at=observed_at,
                    observed_at=observed_at,
                ),
            ]
        )
        for revision, state, vss_state in (
            (base_sha, "completed", "done"),
            (head_sha, "accepted", "running"),
            (merge_sha, "completed", "done"),
        ):
            session.add(
                Snapshot(
                    request_id=uuid4(),
                    binding_id=None,
                    tracked_branch_id=tracked_branch.tracked_branch_id,
                    frontend_project_id=None,
                    repository_id=repository.repository_id,
                    branch_ref=tracked_branch.branch_ref,
                    vss_project_id=tracked_branch.vss_project_id,
                    base_revision=revision,
                    target_revision=revision,
                    source_type="remote_clone",
                    state=state,
                    vss_state=vss_state,
                    materialized_locator=f"context/revisions/{revision}",
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
            snapshot_index_orchestration_mode="vss_pull",
            docs_enabled=False,
        )
    )
    headers = {"X-Snapshot-Token": "shared-secret"}
    with TestClient(app) as client:
        listing = client.get(
            "/v1/internal/vss/change-requests",
            params={"project_id": "change-context--main"},
            headers=headers,
        )
        detail = client.get(
            "/v1/internal/vss/change-requests/github/42",
            params={"project_id": "change-context--main"},
            headers=headers,
        )
        capabilities = client.get(
            "/v1/internal/vss/capabilities",
            headers=headers,
        )
        repositories = client.get(
            "/v1/internal/vss/repositories",
            headers=headers,
        )
        assert repositories.status_code == 200, repositories.text
        repository_id = repositories.json()["items"][0]["repository_id"]
        commit_graph = client.get(
            f"/v1/internal/vss/repositories/{repository_id}/commit-graph",
            params={"limit": 2},
            headers=headers,
        )
        assert commit_graph.status_code == 200, commit_graph.text
        next_cursor = commit_graph.json()["next_cursor"]
        commit_graph_next = client.get(
            f"/v1/internal/vss/repositories/{repository_id}/commit-graph",
            params={"limit": 2, "cursor": next_cursor},
            headers=headers,
        )
        refs = client.get(
            "/v1/internal/vss/refs",
            params={"project_id": "change-context--main"},
            headers=headers,
        )
        branch_context = client.get(
            "/v1/internal/vss/context",
            params={
                "project_id": "change-context--main",
                "branch_ref": "refs/heads/main",
            },
            headers=headers,
        )
        tag_context = client.get(
            "/v1/internal/vss/context",
            params={
                "project_id": "change-context--main",
                "tag_ref": "refs/tags/v1.0.0",
            },
            headers=headers,
        )
        head_context = client.get(
            "/v1/internal/vss/context",
            params={
                "project_id": "change-context--main",
                "change_request_provider": "github",
                "change_request_number": 42,
                "change_request_role": "head",
            },
            headers=headers,
        )

    assert listing.status_code == 200, listing.text
    item = listing.json()["items"][0]
    assert item["provider"] == "github"
    availability = {value["role"]: value for value in item["revisions"]}
    assert availability["base"]["eligible_for_answer"] is True
    assert availability["head"]["eligible_for_answer"] is False
    assert availability["head"]["unavailable_reason"] == "SNAPSHOT_NOT_COMPLETED"
    assert availability["merge"]["eligible_for_answer"] is True
    assert detail.status_code == 200, detail.text
    assert detail.json()["reason"] == "VSS_CHANGE_REQUEST_READY"
    assert len(detail.json()["observations"]) == 2
    assert capabilities.status_code == 200, capabilities.text
    assert capabilities.json()["orchestration_mode"] == "vss_pull"
    assert capabilities.json()["index_start_owner"] == "vss"
    assert capabilities.json()["module_starts_indexing"] is False
    assert "repositories" in capabilities.json()["resources"]
    assert "commit_graph" in capabilities.json()["resources"]
    assert "delta" in capabilities.json()["resources"]
    repository_item = repositories.json()["items"][0]
    assert repository_item["repository_name"] == "h5vision/change-context"
    assert repository_item["branches"][0]["branch_ref"] == "refs/heads/main"
    assert repository_item["branches"][0]["current_head_sha"] == merge_sha
    graph_body = commit_graph.json()
    assert graph_body["reason"] == "VSS_COMMIT_GRAPH_READY"
    assert graph_body["repository_id"] == repository_id
    assert graph_body["branches"][0]["current_head_sha"] == merge_sha
    assert [item["commit_sha"] for item in graph_body["items"]] == [merge_sha, head_sha]
    assert graph_body["items"][0]["parent_shas"] == [head_sha]
    assert graph_body["next_cursor"] == head_sha
    assert commit_graph_next.status_code == 200, commit_graph_next.text
    assert [item["commit_sha"] for item in commit_graph_next.json()["items"]] == [base_sha]
    assert commit_graph_next.json()["next_cursor"] is None
    assert refs.status_code == 200, refs.text
    refs_by_name = {item["ref"]: item for item in refs.json()["items"]}
    assert refs_by_name["refs/heads/main"]["revision"] == merge_sha
    assert refs_by_name["refs/tags/v1.0.0"]["revision"] == merge_sha
    assert refs_by_name["refs/heads/main"]["readiness"]["source_ready"] is True
    assert branch_context.status_code == 200, branch_context.text
    assert branch_context.json()["selected_revision"] == merge_sha
    assert branch_context.json()["selection"]["reason"] == "BRANCH_HEAD"
    assert branch_context.json()["commit"]["subject"] == "merge revision"
    assert branch_context.json()["commit"]["parent_shas"] == [head_sha]
    assert tag_context.status_code == 200, tag_context.text
    assert tag_context.json()["selection"]["reason"] == "TAG_TARGET"
    assert head_context.status_code == 200, head_context.text
    assert head_context.json()["selected_revision"] == head_sha
    assert head_context.json()["selection"]["reason"] == "CHANGE_REQUEST_HEAD"
    assert head_context.json()["readiness"]["index_ready_observed"] is False
    assert "project_root" not in head_context.text


def test_vss_delta_exposes_exact_fast_forward_changes_and_safe_fallback(tmp_path: Path) -> None:
    database_path = tmp_path / "delta.db"
    repository_root = tmp_path / "repos"
    source = tmp_path / "delta-source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.email", "delta@example.invalid")
    git(source, "config", "user.name", "Delta Test")
    git(source, "config", "core.autocrlf", "false")
    (source / "app.py").write_text("VERSION = 1\n", encoding="utf-8")
    (source / "deleted.py").write_text("DELETE_ME = True\n", encoding="utf-8")
    (source / "rename_old.py").write_text("RENAMED = True\n", encoding="utf-8")
    git(source, "add", "--all")
    git(source, "commit", "-m", "delta base")
    base_revision = git(source, "rev-parse", "HEAD")
    base_tree = git(source, "rev-parse", "HEAD^{tree}")

    (source / "app.py").write_text("VERSION = 2\n", encoding="utf-8")
    (source / "added.py").write_text("ADDED = True\n", encoding="utf-8")
    (source / "deleted.py").unlink()
    git(source, "mv", "rename_old.py", "renamed.py")
    git(source, "add", "--all")
    git(source, "commit", "-m", "delta target")
    target_revision = git(source, "rev-parse", "HEAD")
    target_tree = git(source, "rev-parse", "HEAD^{tree}")

    repository_id = uuid4()
    cache = repository_root / ".repository-cache" / f"{repository_id.hex}.git"
    cache.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "--bare", "--quiet", str(source), str(cache)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    engine = create_engine(
        f"sqlite:///{database_path}",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        repository = Repository(
            repository_id=repository_id,
            canonical_name="h5vision/delta",
            display_name="Delta",
            provider="github",
            remote_url="https://github.com/h5vision/delta.git",
            default_branch_ref="refs/heads/main",
        )
        session.add(repository)
        session.flush()
        session.add(
            TrackedBranch(
                repository_id=repository_id,
                branch_ref="refs/heads/main",
                vss_project_id="delta--main",
                current_head_sha=target_revision,
            )
        )
        session.commit()
    engine.dispose()

    app = create_app(
        Settings(
            vision_environment="test",
            database_url=f"sqlite+aiosqlite:///{database_path}",
            snapshot_repository_root=repository_root,
            snapshot_materialization_root=tmp_path / "snapshots",
            snapshot_vss_api_token="shared-secret",
            snapshot_recovery_on_startup=False,
            docs_enabled=False,
        )
    )
    headers = {"X-Snapshot-Token": "shared-secret"}
    with TestClient(app) as client:
        delta = client.get(
            "/v1/internal/vss/delta",
            params={
                "project_id": "delta--main",
                "base_revision": base_revision,
                "target_revision": target_revision,
            },
            headers=headers,
        )
        same = client.get(
            "/v1/internal/vss/delta",
            params={
                "project_id": "delta--main",
                "base_revision": target_revision,
                "target_revision": target_revision,
            },
            headers=headers,
        )
        rewind = client.get(
            "/v1/internal/vss/delta",
            params={
                "project_id": "delta--main",
                "base_revision": target_revision,
                "target_revision": base_revision,
            },
            headers=headers,
        )
        missing_base = client.get(
            "/v1/internal/vss/delta",
            params={
                "project_id": "delta--main",
                "base_revision": "f" * 40,
                "target_revision": target_revision,
            },
            headers=headers,
        )

    assert delta.status_code == 200, delta.text
    body = delta.json()
    assert body["reason"] == "VSS_DELTA_READY"
    assert body["relationship"] == "fast_forward"
    assert body["delta_complete"] is True
    assert body["full_reindex_required"] is False
    assert body["base_tree_sha"] == base_tree
    assert body["target_tree_sha"] == target_tree
    changes = {item["path"]: item for item in body["changes"]}
    assert changes["app.py"]["status"] == "modified"
    assert changes["added.py"]["status"] == "added"
    assert changes["deleted.py"]["status"] == "deleted"
    assert changes["renamed.py"] == {
        "status": "renamed",
        "path": "renamed.py",
        "old_path": "rename_old.py",
    }

    assert same.status_code == 200, same.text
    assert same.json()["relationship"] == "same"
    assert same.json()["changes"] == []
    assert same.json()["full_reindex_required"] is False

    assert rewind.status_code == 200, rewind.text
    assert rewind.json()["reason"] == "VSS_DELTA_FULL_REINDEX_REQUIRED"
    assert rewind.json()["relationship"] == "diverged"
    assert rewind.json()["full_reindex_required"] is True
    assert rewind.json()["changes"] == []

    assert missing_base.status_code == 200, missing_base.text
    assert missing_base.json()["relationship"] == "unknown"
    assert missing_base.json()["fallback_reason"] == "COMPARE_REVISION_NOT_FOUND"
    assert missing_base.json()["full_reindex_required"] is True


def test_vss_context_requires_exactly_one_complete_selector(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            vision_environment="test",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'empty-context.db'}",
            snapshot_materialization_root=tmp_path / "snapshots",
            snapshot_vss_api_token="shared-secret",
            snapshot_recovery_on_startup=False,
            docs_enabled=False,
        )
    )
    headers = {"X-Snapshot-Token": "shared-secret"}
    with TestClient(app) as client:
        missing = client.get(
            "/v1/internal/vss/context",
            params={"project_id": "missing"},
            headers=headers,
        )
        ambiguous = client.get(
            "/v1/internal/vss/context",
            params={
                "project_id": "missing",
                "revision": "1" * 40,
                "branch_ref": "refs/heads/main",
            },
            headers=headers,
        )
        incomplete_change_request = client.get(
            "/v1/internal/vss/context",
            params={
                "project_id": "missing",
                "change_request_provider": "github",
                "change_request_number": 42,
            },
            headers=headers,
        )

    for response in (missing, ambiguous, incomplete_change_request):
        assert response.status_code == 422
        assert response.json()["reason"] == "VSS_CONTEXT_SELECTOR_INVALID"


def test_openapi_exposes_the_vss_pull_provider_contract(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            vision_environment="test",
            snapshot_materialization_root=tmp_path / "snapshots",
            snapshot_recovery_on_startup=False,
            docs_enabled=False,
        )
    )
    with TestClient(app) as client:
        openapi = client.get("/openapi.json").json()

    paths = openapi["paths"]
    for path in (
        "/v1/internal/vss/capabilities",
        "/v1/internal/vss/repositories",
        "/v1/internal/vss/repositories/{repository_id}/commit-graph",
        "/v1/internal/vss/delta",
        "/v1/internal/vss/refs",
        "/v1/internal/vss/context",
        "/v1/internal/vss/source",
        "/v1/internal/vss/revisions",
        "/v1/internal/vss/change-requests",
        "/v1/internal/vss/change-requests/{provider}/{external_number}",
    ):
        assert path in paths
