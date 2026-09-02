"""Snapshot ORM model with (vss_project_id, target_revision) idempotency constraint."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.infrastructure.database.base import Base

if TYPE_CHECKING:
    from backend.infrastructure.database.models.attempt import SnapshotAttempt
    from backend.infrastructure.database.models.binding import BranchBinding
    from backend.infrastructure.database.models.collection import TrackedBranch
    from backend.infrastructure.database.models.delta import SnapshotDelta
    from backend.infrastructure.database.models.repository import Repository


class Snapshot(Base):
    __tablename__ = "snapshots"

    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    request_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
    )
    binding_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("branch_bindings.binding_id", ondelete="RESTRICT"),
        nullable=True,
    )
    tracked_branch_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tracked_branches.tracked_branch_id", ondelete="RESTRICT"),
        nullable=True,
    )
    frontend_project_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    repository_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("repositories.repository_id", ondelete="RESTRICT"),
        nullable=False,
    )
    branch_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    vss_project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    base_revision: Mapped[str] = mapped_column(String(40), nullable=False)
    target_revision: Mapped[str] = mapped_column(String(40), nullable=False)
    source_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="client_local_git",
    )
    state: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="received",
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    materialized_locator: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    vss_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vss_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vss_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
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

    # Relationships
    repository: Mapped[Repository] = relationship(
        "Repository",
        back_populates="snapshots",
    )
    binding: Mapped[BranchBinding | None] = relationship(
        "BranchBinding",
        back_populates="snapshots",
    )
    tracked_branch: Mapped[TrackedBranch | None] = relationship(
        "TrackedBranch",
        back_populates="snapshots",
    )
    deltas: Mapped[list[SnapshotDelta]] = relationship(
        "SnapshotDelta",
        back_populates="snapshot",
    )
    attempts: Mapped[list[SnapshotAttempt]] = relationship(
        "SnapshotAttempt",
        back_populates="snapshot",
        order_by="SnapshotAttempt.attempt_number",
    )

    __table_args__ = (
        # Idempotency constraint: One snapshot per (vss_project_id, target_revision)
        UniqueConstraint(
            "vss_project_id",
            "target_revision",
            name="uq_snapshots_vss_project_target_revision",
        ),
        CheckConstraint("length(base_revision) = 40", name="ck_snapshots_base_revision_length"),
        CheckConstraint(
            "length(target_revision) = 40",
            name="ck_snapshots_target_revision_length",
        ),
        CheckConstraint(
            "source_type IN ('client_local_git', 'remote_clone', 'prior_revision', "
            "'bootstrap_full')",
            name="ck_snapshots_source_type",
        ),
        CheckConstraint(
            "state IN ('received', 'validated', 'binding_required', 'materializing', "
            "'materialized', 'submitting', 'accepted', 'indexing', 'already_indexed', "
            "'completed', 'rejected', 'failed', 'aborted')",
            name="ck_snapshots_state",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_snapshots_attempt_count"),
        CheckConstraint(
            "(binding_id IS NOT NULL AND tracked_branch_id IS NULL AND "
            "frontend_project_id IS NOT NULL) OR "
            "(binding_id IS NULL AND tracked_branch_id IS NOT NULL AND "
            "frontend_project_id IS NULL)",
            name="ck_snapshots_exact_source_owner",
        ),
        Index("ix_snapshots_repo_branch_target", "repository_id", "branch_ref", "target_revision"),
        Index("ix_snapshots_tracked_branch", "tracked_branch_id", "created_at"),
        Index("ix_snapshots_state", "state"),
        Index("ix_snapshots_created_at", "created_at"),
    )
