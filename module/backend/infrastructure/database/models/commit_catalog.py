"""Repository commit graph and resumable catalog run models."""

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
    from backend.infrastructure.database.models.repository import Repository


class RepositoryCommit(Base):
    """Verified commit metadata for one Repository."""

    __tablename__ = "repository_commits"

    repository_commit_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("repositories.repository_id", ondelete="RESTRICT"),
        nullable=False,
    )
    commit_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    tree_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    author_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    authored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    object_verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    repository: Mapped[Repository] = relationship("Repository", back_populates="commits")
    parents: Mapped[list[RepositoryCommitParent]] = relationship(
        "RepositoryCommitParent",
        foreign_keys="RepositoryCommitParent.repository_commit_id",
        back_populates="repository_commit",
        order_by="RepositoryCommitParent.parent_order",
    )

    __table_args__ = (
        UniqueConstraint(
            "repository_id", "commit_sha", name="uq_repository_commits_repository_sha"
        ),
        CheckConstraint("length(commit_sha) = 40", name="ck_repository_commits_sha_length"),
        CheckConstraint("length(tree_sha) = 40", name="ck_repository_commits_tree_length"),
        Index("ix_repository_commits_repository_committed", "repository_id", "committed_at"),
    )


class RepositoryCommitParent(Base):
    """Ordered parent edge with an external SHA fallback for incomplete graphs."""

    __tablename__ = "repository_commit_parents"

    repository_commit_parent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    repository_commit_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("repository_commits.repository_commit_id", ondelete="RESTRICT"),
        nullable=False,
    )
    parent_commit_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("repository_commits.repository_commit_id", ondelete="RESTRICT"),
        nullable=True,
    )
    parent_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    parent_order: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_missing_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    repository_commit: Mapped[RepositoryCommit] = relationship(
        "RepositoryCommit",
        foreign_keys=[repository_commit_id],
        back_populates="parents",
    )

    __table_args__ = (
        UniqueConstraint(
            "repository_commit_id",
            "parent_order",
            name="uq_repository_commit_parents_order",
        ),
        UniqueConstraint(
            "repository_commit_id",
            "parent_sha",
            name="uq_repository_commit_parents_sha",
        ),
        CheckConstraint("length(parent_sha) = 40", name="ck_repository_commit_parents_sha"),
        CheckConstraint("parent_order >= 0", name="ck_repository_commit_parents_order"),
        CheckConstraint(
            "parent_missing_reason IS NULL OR parent_missing_reason IN "
            "('scan_truncated', 'shallow_history', 'object_unavailable')",
            name="ck_repository_commit_parents_missing_reason",
        ),
        CheckConstraint(
            "(parent_commit_id IS NOT NULL AND parent_missing_reason IS NULL) OR "
            "(parent_commit_id IS NULL AND parent_missing_reason IS NOT NULL)",
            name="ck_repository_commit_parents_resolution",
        ),
        Index("ix_repository_commit_parents_parent_sha", "parent_sha"),
    )


class CommitCatalogRun(Base):
    """Repository-level bounded catalog execution and lease."""

    __tablename__ = "commit_catalog_runs"

    run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    request_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    repository_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("repositories.repository_id", ondelete="RESTRICT"),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    roots_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    unavailable_roots_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    max_commits: Mapped[int] = mapped_column(Integer, nullable=False)
    discovered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    persisted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    shallow: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    history_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    repository: Mapped[Repository] = relationship(
        "Repository", back_populates="commit_catalog_runs"
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('running', 'succeeded', 'failed')", name="ck_commit_catalog_runs_state"
        ),
        CheckConstraint("max_commits > 0", name="ck_commit_catalog_runs_max_commits"),
        CheckConstraint(
            "discovered_count >= 0 AND persisted_count >= 0",
            name="ck_commit_catalog_runs_counts",
        ),
        CheckConstraint(
            "(state = 'running' AND finished_at IS NULL) OR "
            "(state <> 'running' AND finished_at IS NOT NULL)",
            name="ck_commit_catalog_runs_finished_state",
        ),
        Index(
            "uq_commit_catalog_runs_active_repository",
            "repository_id",
            unique=True,
            postgresql_where=(state == "running"),
            sqlite_where=(state == "running"),
        ),
        Index("ix_commit_catalog_runs_repository_started", "repository_id", "started_at"),
    )
