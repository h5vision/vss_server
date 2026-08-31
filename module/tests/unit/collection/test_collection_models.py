"""Unit tests for Collection models (TrackedBranch, BranchHeadHistory, RepositorySyncRun)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.infrastructure.database.base import Base
from backend.infrastructure.database.models import (
    BranchHeadHistory,
    Repository,
    RepositorySyncRun,
    TrackedBranch,
)


@pytest.fixture
def db_session() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        execution_options={"schema_translate_map": {"snapshot": None}},
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(engine)


def test_tracked_branch_lifecycle(db_session: Session) -> None:
    repo = Repository(
        canonical_name="h5vision/test-repo",
        display_name="Test Repo",
        provider="github",
        remote_url="https://github.com/h5vision/test.git",
        default_branch_ref="refs/heads/main",
        active=True,
    )
    db_session.add(repo)
    db_session.commit()

    tracked = TrackedBranch(
        repository_id=repo.repository_id,
        branch_ref="refs/heads/main",
        vss_project_id="prj_test_1",
        current_head_sha="a" * 40,
        tracked=True,
    )
    db_session.add(tracked)
    db_session.commit()

    saved = db_session.scalar(
        select(TrackedBranch).where(TrackedBranch.tracked_branch_id == tracked.tracked_branch_id)
    )
    assert saved is not None
    assert saved.branch_ref == "refs/heads/main"
    assert saved.current_head_sha == "a" * 40
    assert saved.tracked is True


def test_branch_head_history_recording(db_session: Session) -> None:
    repo = Repository(
        canonical_name="h5vision/test-history",
        display_name="Test History Repo",
        provider="github",
        remote_url="https://github.com/h5vision/test.git",
        default_branch_ref="refs/heads/main",
        active=True,
    )
    db_session.add(repo)
    db_session.commit()

    tracked = TrackedBranch(
        repository_id=repo.repository_id,
        branch_ref="refs/heads/main",
        vss_project_id="prj_hist_1",
        tracked=True,
    )
    db_session.add(tracked)
    db_session.commit()

    run = RepositorySyncRun(
        repository_id=repo.repository_id,
        trigger="manual",
        state="running",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(run)
    db_session.commit()

    hist1 = BranchHeadHistory(
        tracked_branch_id=tracked.tracked_branch_id,
        previous_head_sha=None,
        observed_head_sha="1" * 40,
        change_type="initial",
        sync_run_id=run.sync_run_id,
    )
    hist2 = BranchHeadHistory(
        tracked_branch_id=tracked.tracked_branch_id,
        previous_head_sha="1" * 40,
        observed_head_sha="2" * 40,
        change_type="fast_forward",
        sync_run_id=run.sync_run_id,
    )
    db_session.add_all([hist1, hist2])
    db_session.commit()

    entries = list(
        db_session.scalars(
            select(BranchHeadHistory)
            .where(BranchHeadHistory.tracked_branch_id == tracked.tracked_branch_id)
            .order_by(BranchHeadHistory.observed_at)
        )
    )
    assert len(entries) == 2
    assert entries[0].change_type == "initial"
    assert entries[0].observed_head_sha == "1" * 40
    assert entries[1].change_type == "fast_forward"
    assert entries[1].observed_head_sha == "2" * 40
