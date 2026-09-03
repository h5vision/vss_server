"""Repository Tag current state and append-only revision history."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.infrastructure.database.base import Base

if TYPE_CHECKING:
    from backend.infrastructure.database.models.repository import Repository


class RepositoryTag(Base):
    __tablename__ = "repository_tags"

    repository_tag_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("repositories.repository_id", ondelete="RESTRICT"),
        nullable=False,
    )
    tag_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    current_commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    repository: Mapped[Repository] = relationship("Repository", back_populates="tags")
    history: Mapped[list[TagRevisionHistory]] = relationship(
        "TagRevisionHistory",
        back_populates="repository_tag",
        order_by="TagRevisionHistory.observed_at",
    )

    __table_args__ = (
        UniqueConstraint(
            "repository_id", "tag_ref", name="uq_repository_tags_repository_ref"
        ),
        CheckConstraint(
            "tag_ref LIKE 'refs/tags/%'", name="ck_repository_tags_ref_prefix"
        ),
        CheckConstraint(
            "current_commit_sha IS NULL OR length(current_commit_sha) = 40",
            name="ck_repository_tags_current_sha",
        ),
        Index("ix_repository_tags_repository_current", "repository_id", "current_commit_sha"),
    )


class TagRevisionHistory(Base):
    __tablename__ = "tag_revision_history"

    tag_history_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    repository_tag_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("repository_tags.repository_tag_id", ondelete="RESTRICT"),
        nullable=False,
    )
    sync_run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("repository_sync_runs.sync_run_id", ondelete="RESTRICT"),
        nullable=False,
    )
    previous_commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    observed_commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    change_type: Mapped[str] = mapped_column(String(16), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    repository_tag: Mapped[RepositoryTag] = relationship(
        "RepositoryTag", back_populates="history"
    )

    __table_args__ = (
        CheckConstraint(
            "previous_commit_sha IS NULL OR length(previous_commit_sha) = 40",
            name="ck_tag_revision_history_previous_sha",
        ),
        CheckConstraint(
            "observed_commit_sha IS NULL OR length(observed_commit_sha) = 40",
            name="ck_tag_revision_history_observed_sha",
        ),
        CheckConstraint(
            "NOT (previous_commit_sha IS NULL AND observed_commit_sha IS NULL)",
            name="ck_tag_revision_history_has_revision",
        ),
        CheckConstraint(
            "change_type IN ('created', 'moved', 'deleted', 'recreated')",
            name="ck_tag_revision_history_change_type",
        ),
        Index(
            "ix_tag_revision_history_tag_observed",
            "repository_tag_id",
            "observed_at",
        ),
    )
