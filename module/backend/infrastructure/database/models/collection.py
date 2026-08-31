"""Repository·Branch 수집 코어의 정본 모델.

`frontend_project_id` 중심 BranchBinding은 Frontend 호환 계층이고, Repository와
Branch, VSS project의 수집 관계는 이 모듈의 tracked_branches가 소유한다. HEAD
이력과 sync run은 append-only 감사 레코드이므로 어떤 경로에서도 물리 삭제하지
않는다.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
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


class RepositorySyncRun(Base):
    """한 Repository의 수집 동기화 실행 감사 레코드."""

    __tablename__ = "repository_sync_runs"

    sync_run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("repositories.repository_id", ondelete="RESTRICT"),
        nullable=False,
    )
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    repository: Mapped[Repository] = relationship(
        "Repository",
        lazy="raise",
    )

    __table_args__ = (
        CheckConstraint(
            "trigger IN ('manual', 'periodic', 'startup')",
            name="ck_repository_sync_runs_trigger",
        ),
        CheckConstraint(
            "state IN ('running', 'succeeded', 'failed')",
            name="ck_repository_sync_runs_state",
        ),
        Index("ix_repository_sync_runs_repo_started", "repository_id", "started_at"),
        Index(
            "uq_repository_sync_runs_running_per_repository",
            "repository_id",
            unique=True,
            postgresql_where=(state == "running"),
            sqlite_where=(state == "running"),
        ),
    )


class TrackedBranch(Base):
    """사용자가 선택한 exact branch ref와 VSS project의 수집 연결."""

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
    tracked: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    current_head_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
        lazy="raise",
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
        UniqueConstraint("repository_id", "branch_ref", name="uq_tracked_branches_repo_ref"),
        CheckConstraint(
            "branch_ref LIKE 'refs/heads/%'",
            name="ck_tracked_branches_branch_ref_prefix",
        ),
        CheckConstraint(
            "current_head_sha IS NULL OR length(current_head_sha) = 40",
            name="ck_tracked_branches_head_sha_length",
        ),
        Index("ix_tracked_branches_repository_tracked", "repository_id", "tracked"),
    )


class BranchHeadHistory(Base):
    """브랜치 HEAD 변경의 append-only 관측 기록."""

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
    previous_head_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    observed_head_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    change_type: Mapped[str] = mapped_column(String(32), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    sync_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("repository_sync_runs.sync_run_id", ondelete="SET NULL"),
        nullable=True,
    )

    tracked_branch: Mapped[TrackedBranch] = relationship(
        "TrackedBranch",
        back_populates="head_history",
    )

    __table_args__ = (
        CheckConstraint(
            "change_type IN ('initial', 'fast_forward', 'rewind', 'branch_deleted')",
            name="ck_branch_head_history_change_type",
        ),
        CheckConstraint(
            "previous_head_sha IS NULL OR length(previous_head_sha) = 40",
            name="ck_branch_head_history_previous_sha_length",
        ),
        CheckConstraint(
            "observed_head_sha IS NULL OR length(observed_head_sha) = 40",
            name="ck_branch_head_history_observed_sha_length",
        ),
        # force-push와 브랜치 삭제도 이력을 남긴다. 삭제 관측은 새 HEAD가 없으므로
        # observed_head_sha를 비워 두고 다른 change_type은 항상 관측 SHA를 요구한다.
        CheckConstraint(
            "(change_type = 'branch_deleted' AND observed_head_sha IS NULL) OR "
            "(change_type <> 'branch_deleted' AND observed_head_sha IS NOT NULL)",
            name="ck_branch_head_history_observed_sha_presence",
        ),
        Index(
            "ix_branch_head_history_branch_observed",
            "tracked_branch_id",
            "observed_at",
        ),
    )
