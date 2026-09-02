"""Repository/Branch 수집 상태와 append-only HEAD 관측 이력 모델."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.infrastructure.database.base import Base

if TYPE_CHECKING:
    from backend.infrastructure.database.models.repository import Repository
    from backend.infrastructure.database.models.snapshot import Snapshot


class TrackedBranch(Base):
    """사용자가 명시적으로 선택한 Repository Branch의 현재 수집 상태."""

    __tablename__ = "tracked_branches"

    tracked_branch_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("repositories.repository_id", ondelete="RESTRICT"),
        nullable=False,
    )
    branch_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    vss_project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    current_head_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    tracked: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    repository: Mapped[Repository] = relationship(
        "Repository",
        back_populates="tracked_branches",
    )
    head_history: Mapped[list[BranchHeadHistory]] = relationship(
        "BranchHeadHistory",
        back_populates="tracked_branch",
        order_by="BranchHeadHistory.observed_at",
    )
    snapshots: Mapped[list[Snapshot]] = relationship(
        "Snapshot",
        back_populates="tracked_branch",
    )

    __table_args__ = (
        UniqueConstraint(
            "repository_id",
            "branch_ref",
            name="uq_tracked_branches_repository_ref",
        ),
        UniqueConstraint(
            "vss_project_id",
            name="uq_tracked_branches_vss_project_id",
        ),
        CheckConstraint(
            "current_head_sha IS NULL OR length(current_head_sha) = 40",
            name="ck_tracked_branches_current_head_length",
        ),
        Index("ix_tracked_branches_repository_tracked", "repository_id", "tracked"),
    )


class RepositorySyncRun(Base):
    """수동·정기 수집이 공유하는 저장소 단위 실행 기록과 lease."""

    __tablename__ = "repository_sync_runs"

    sync_run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    request_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    repository_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("repositories.repository_id", ondelete="RESTRICT"),
        nullable=False,
    )
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    result_json: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    repository: Mapped[Repository] = relationship(
        "Repository",
        back_populates="sync_runs",
    )
    head_history: Mapped[list[BranchHeadHistory]] = relationship(
        "BranchHeadHistory",
        back_populates="sync_run",
    )

    __table_args__ = (
        CheckConstraint(
            "trigger IN ('manual', 'periodic')",
            name="ck_repository_sync_runs_trigger",
        ),
        CheckConstraint(
            "state IN ('running', 'succeeded', 'failed')",
            name="ck_repository_sync_runs_state",
        ),
        CheckConstraint(
            "(state = 'running' AND finished_at IS NULL) OR "
            "(state <> 'running' AND finished_at IS NOT NULL)",
            name="ck_repository_sync_runs_finished_state",
        ),
        Index(
            "uq_repository_sync_runs_active_repository",
            "repository_id",
            unique=True,
            postgresql_where=(state == "running"),
            sqlite_where=(state == "running"),
        ),
        Index("ix_repository_sync_runs_started", "repository_id", "started_at"),
    )


class BranchHeadHistory(Base):
    """Branch HEAD 변화만 보존하는 append-only 관측 이력."""

    __tablename__ = "branch_head_history"

    history_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    tracked_branch_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tracked_branches.tracked_branch_id", ondelete="RESTRICT"),
        nullable=False,
    )
    sync_run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("repository_sync_runs.sync_run_id", ondelete="RESTRICT"),
        nullable=False,
    )
    previous_head_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    observed_head_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    change_type: Mapped[str] = mapped_column(String(32), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    tracked_branch: Mapped[TrackedBranch] = relationship(
        "TrackedBranch",
        back_populates="head_history",
    )
    sync_run: Mapped[RepositorySyncRun] = relationship(
        "RepositorySyncRun",
        back_populates="head_history",
    )

    __table_args__ = (
        CheckConstraint(
            "previous_head_sha IS NULL OR length(previous_head_sha) = 40",
            name="ck_branch_head_history_previous_length",
        ),
        CheckConstraint(
            "observed_head_sha IS NULL OR length(observed_head_sha) = 40",
            name="ck_branch_head_history_observed_length",
        ),
        CheckConstraint(
            "change_type IN ('created', 'fast_forward', 'rewind', 'deleted', 'recreated')",
            name="ck_branch_head_history_change_type",
        ),
        CheckConstraint(
            "NOT (previous_head_sha IS NULL AND observed_head_sha IS NULL)",
            name="ck_branch_head_history_has_revision",
        ),
        Index(
            "ix_branch_head_history_branch_observed",
            "tracked_branch_id",
            "observed_at",
        ),
    )
