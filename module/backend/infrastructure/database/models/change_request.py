"""Provider-neutral PR/MR current state and append-only revision observations."""

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
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.infrastructure.database.base import Base

if TYPE_CHECKING:
    from backend.infrastructure.database.models.repository import Repository


class ChangeRequest(Base):
    """Latest normalized state for one GitHub PR or GitLab MR."""

    __tablename__ = "change_requests"

    change_request_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("repositories.repository_id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_number: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    base_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    head_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    current_base_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    current_head_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    current_merge_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provider_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    repository: Mapped[Repository] = relationship(
        "Repository", back_populates="change_requests"
    )
    revisions: Mapped[list[ChangeRequestRevision]] = relationship(
        "ChangeRequestRevision",
        back_populates="change_request",
        order_by="ChangeRequestRevision.observed_at",
    )

    __table_args__ = (
        UniqueConstraint(
            "repository_id",
            "provider",
            "external_number",
            name="uq_change_requests_repository_provider_number",
        ),
        CheckConstraint("provider IN ('github', 'gitlab')", name="ck_change_requests_provider"),
        CheckConstraint(
            "kind IN ('pull_request', 'merge_request')", name="ck_change_requests_kind"
        ),
        CheckConstraint(
            "(provider = 'github' AND kind = 'pull_request') OR "
            "(provider = 'gitlab' AND kind = 'merge_request')",
            name="ck_change_requests_provider_kind",
        ),
        CheckConstraint(
            "state IN ('open', 'closed', 'merged')", name="ck_change_requests_state"
        ),
        CheckConstraint("external_number > 0", name="ck_change_requests_external_number"),
        CheckConstraint(
            "base_ref LIKE 'refs/heads/%' AND head_ref LIKE 'refs/heads/%'",
            name="ck_change_requests_branch_refs",
        ),
        CheckConstraint(
            "length(current_base_sha) = 40", name="ck_change_requests_base_sha_length"
        ),
        CheckConstraint(
            "length(current_head_sha) = 40", name="ck_change_requests_head_sha_length"
        ),
        CheckConstraint(
            "current_merge_sha IS NULL OR length(current_merge_sha) = 40",
            name="ck_change_requests_merge_sha_length",
        ),
        CheckConstraint(
            "(state = 'merged' AND current_merge_sha IS NOT NULL AND merged_at IS NOT NULL) OR "
            "(state <> 'merged' AND current_merge_sha IS NULL AND merged_at IS NULL)",
            name="ck_change_requests_merge_state",
        ),
        Index("ix_change_requests_repository_state", "repository_id", "state"),
    )


class ChangeRequestRevision(Base):
    """Append-only normalized base/head/merge observation for a PR or MR."""

    __tablename__ = "change_request_revisions"

    revision_observation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    change_request_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("change_requests.change_request_id", ondelete="RESTRICT"),
        nullable=False,
    )
    observation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    base_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    head_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    base_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    merge_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    provider_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    change_request: Mapped[ChangeRequest] = relationship(
        "ChangeRequest", back_populates="revisions"
    )

    __table_args__ = (
        UniqueConstraint(
            "change_request_id",
            "observation_key",
            name="uq_change_request_revisions_observation",
        ),
        CheckConstraint(
            "state IN ('open', 'closed', 'merged')",
            name="ck_change_request_revisions_state",
        ),
        CheckConstraint(
            "base_ref LIKE 'refs/heads/%' AND head_ref LIKE 'refs/heads/%'",
            name="ck_change_request_revisions_branch_refs",
        ),
        CheckConstraint("length(base_sha) = 40", name="ck_change_request_revisions_base_sha"),
        CheckConstraint("length(head_sha) = 40", name="ck_change_request_revisions_head_sha"),
        CheckConstraint(
            "merge_sha IS NULL OR length(merge_sha) = 40",
            name="ck_change_request_revisions_merge_sha",
        ),
        CheckConstraint(
            "(state = 'merged' AND merge_sha IS NOT NULL) OR "
            "(state <> 'merged' AND merge_sha IS NULL)",
            name="ck_change_request_revisions_merge_state",
        ),
        Index(
            "ix_change_request_revisions_request_observed",
            "change_request_id",
            "observed_at",
        ),
    )
