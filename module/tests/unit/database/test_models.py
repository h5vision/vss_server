"""Unit tests for SQLAlchemy models, constraints, and relationships."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.infrastructure.database.base import Base
from backend.infrastructure.database.models import (
    AuditLog,
    BranchBinding,
    Repository,
    Snapshot,
    SnapshotAttempt,
    SnapshotDelta,
)


@pytest.fixture
def db_session() -> Session:
    # Use SQLite in-memory engine for unit testing models and constraints
    engine = create_engine(
        "sqlite:///:memory:",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(engine)


def test_repository_model_creation_and_fields(db_session: Session) -> None:
    assert all(table.schema == "snapshot" for table in Base.metadata.tables.values())

    repo = Repository(
        canonical_name="h5vision/vision",
        display_name="Vision Main Repository",
        provider="github",
        remote_url="https://github.com/h5vision/vision.git",
        default_branch_ref="refs/heads/main",
        active=True,
    )
    db_session.add(repo)
    db_session.commit()

    saved = db_session.scalar(
        select(Repository).where(Repository.canonical_name == "h5vision/vision")
    )
    assert saved is not None
    assert saved.display_name == "Vision Main Repository"
    assert saved.provider == "github"
    assert saved.active is True
    assert saved.repository_id is not None
    assert isinstance(saved.repository_id, uuid.UUID)


def test_branch_binding_active_partial_uniqueness(db_session: Session) -> None:
    repo = Repository(
        canonical_name="h5vision/vision",
        display_name="Vision",
        provider="github",
        remote_url="https://github.com/h5vision/vision.git",
        default_branch_ref="refs/heads/main",
    )
    db_session.add(repo)
    db_session.flush()

    binding1 = BranchBinding(
        frontend_project_id="frontend-1",
        repository_id=repo.repository_id,
        branch_ref="refs/heads/main",
        vss_project_id="vss-proj-main",
        active=True,
    )
    db_session.add(binding1)
    db_session.commit()

    # Inactive binding with the same frontend_project_id is allowed
    binding_inactive = BranchBinding(
        frontend_project_id="frontend-1",
        repository_id=repo.repository_id,
        branch_ref="refs/heads/feature",
        vss_project_id="vss-proj-feature",
        active=False,
    )
    db_session.add(binding_inactive)
    db_session.commit()

    # Second active binding with the same frontend_project_id must violate partial unique index
    binding2 = BranchBinding(
        frontend_project_id="frontend-1",
        repository_id=repo.repository_id,
        branch_ref="refs/heads/release",
        vss_project_id="vss-proj-release",
        active=True,
    )
    db_session.add(binding2)
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_branch_binding_workspace_name_is_unique_while_active(db_session: Session) -> None:
    repo = Repository(
        canonical_name="h5vision/workspaces",
        display_name="Workspaces",
        provider="github",
        remote_url="https://github.com/h5vision/workspaces.git",
        default_branch_ref="refs/heads/main",
    )
    db_session.add(repo)
    db_session.flush()
    db_session.add(
        BranchBinding(
            frontend_project_id="h5vision/one",
            frontend_workspace_name="vision",
            repository_id=repo.repository_id,
            branch_ref="refs/heads/one",
            vss_project_id="one--main",
            active=True,
        )
    )
    db_session.commit()
    db_session.add(
        BranchBinding(
            frontend_project_id="h5vision/two",
            frontend_workspace_name="vision",
            repository_id=repo.repository_id,
            branch_ref="refs/heads/two",
            vss_project_id="two--main",
            active=True,
        )
    )

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_snapshot_idempotency_constraint(db_session: Session) -> None:
    repo = Repository(
        canonical_name="h5vision/vision",
        display_name="Vision",
        provider="github",
        remote_url="https://github.com/h5vision/vision.git",
        default_branch_ref="refs/heads/main",
    )
    db_session.add(repo)
    db_session.flush()

    binding = BranchBinding(
        frontend_project_id="frontend-1",
        repository_id=repo.repository_id,
        branch_ref="refs/heads/main",
        vss_project_id="vss-proj-main",
        active=True,
    )
    db_session.add(binding)
    db_session.flush()

    target_sha = "a" * 40
    snap1 = Snapshot(
        request_id=uuid.uuid4(),
        binding_id=binding.binding_id,
        frontend_project_id="frontend-1",
        repository_id=repo.repository_id,
        branch_ref="refs/heads/main",
        vss_project_id="vss-proj-main",
        base_revision="0" * 40,
        target_revision=target_sha,
        source_type="client_local_git",
        state="received",
    )
    db_session.add(snap1)
    db_session.commit()

    # Second snapshot with identical (vss_project_id, target_revision) must fail
    snap2 = Snapshot(
        request_id=uuid.uuid4(),
        binding_id=binding.binding_id,
        frontend_project_id="frontend-1",
        repository_id=repo.repository_id,
        branch_ref="refs/heads/main",
        vss_project_id="vss-proj-main",
        base_revision="0" * 40,
        target_revision=target_sha,
        source_type="client_local_git",
        state="received",
    )
    db_session.add(snap2)
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_snapshot_retains_deltas_and_attempts_from_implicit_delete(db_session: Session) -> None:
    repo = Repository(
        canonical_name="h5vision/vision",
        display_name="Vision",
        provider="github",
        remote_url="https://github.com/h5vision/vision.git",
        default_branch_ref="refs/heads/main",
    )
    db_session.add(repo)
    db_session.flush()

    binding = BranchBinding(
        frontend_project_id="frontend-1",
        repository_id=repo.repository_id,
        branch_ref="refs/heads/main",
        vss_project_id="vss-proj-main",
        active=True,
    )
    db_session.add(binding)
    db_session.flush()

    snap = Snapshot(
        request_id=uuid.uuid4(),
        binding_id=binding.binding_id,
        frontend_project_id="frontend-1",
        repository_id=repo.repository_id,
        branch_ref="refs/heads/main",
        vss_project_id="vss-proj-main",
        base_revision="0" * 40,
        target_revision="1" * 40,
        source_type="client_local_git",
        state="accepted",
    )
    db_session.add(snap)
    db_session.flush()

    delta = SnapshotDelta(
        snapshot_id=snap.snapshot_id,
        status="modified",
        path="src/main.py",
        encoding="utf-8",
        content="print('hello')",
    )
    attempt = SnapshotAttempt(
        snapshot_id=snap.snapshot_id,
        request_id=snap.request_id,
        attempt_number=1,
        started_at=datetime.now(UTC),
        upstream_status_code=202,
        vss_state="running",
        vss_result_json={"accepted": True},
    )
    db_session.add_all([delta, attempt])
    db_session.commit()

    assert len(db_session.scalars(select(SnapshotDelta)).all()) == 1
    assert len(db_session.scalars(select(SnapshotAttempt)).all()) == 1

    # Physical deletion is blocked until a retention policy is approved.
    db_session.delete(snap)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    assert len(db_session.scalars(select(SnapshotDelta)).all()) == 1
    assert len(db_session.scalars(select(SnapshotAttempt)).all()) == 1


def test_snapshot_state_and_revision_constraints(db_session: Session) -> None:
    repo = Repository(
        canonical_name="h5vision/constrained",
        display_name="Constrained",
        provider="github",
        remote_url="https://github.com/h5vision/constrained.git",
        default_branch_ref="refs/heads/main",
    )
    db_session.add(repo)
    db_session.flush()
    binding = BranchBinding(
        frontend_project_id="constrained",
        repository_id=repo.repository_id,
        branch_ref="refs/heads/main",
        vss_project_id="constrained--main",
    )
    db_session.add(binding)
    db_session.flush()

    db_session.add(
        Snapshot(
            request_id=uuid.uuid4(),
            binding_id=binding.binding_id,
            frontend_project_id="constrained",
            repository_id=repo.repository_id,
            branch_ref="refs/heads/main",
            vss_project_id="constrained--main",
            base_revision="short",
            target_revision="a" * 40,
            source_type="client_local_git",
            state="unknown",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_audit_log_creation(db_session: Session) -> None:
    audit = AuditLog(
        request_id=uuid.uuid4(),
        actor="admin@example.com",
        action="repository.create",
        target_type="repository",
        target_id="h5vision/vision",
        details={"ip": "127.0.0.1"},
    )
    db_session.add(audit)
    db_session.commit()

    saved = db_session.scalar(select(AuditLog).where(AuditLog.actor == "admin@example.com"))
    assert saved is not None
    assert saved.action == "repository.create"
    assert saved.details == {"ip": "127.0.0.1"}
