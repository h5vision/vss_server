"""BranchBinding ORM model with partial unique index on active frontend projects."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, and_, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.infrastructure.database.base import Base

if TYPE_CHECKING:
    from backend.infrastructure.database.models.repository import Repository
    from backend.infrastructure.database.models.snapshot import Snapshot


class BranchBinding(Base):
    __tablename__ = "branch_bindings"

    binding_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    frontend_project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    frontend_workspace_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    repository_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("repositories.repository_id", ondelete="RESTRICT"),
        nullable=False,
    )
    branch_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    vss_project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(
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

    # Relationships
    repository: Mapped[Repository] = relationship(
        "Repository",
        back_populates="branch_bindings",
    )
    snapshots: Mapped[list[Snapshot]] = relationship(
        "Snapshot",
        back_populates="binding",
    )

    __table_args__ = (
        # Partial unique index: At most one ACTIVE binding per frontend_project_id
        Index(
            "uq_branch_bindings_active_frontend_project",
            "frontend_project_id",
            unique=True,
            postgresql_where=(active.is_(True)),
            sqlite_where=(active == 1),
        ),
        Index(
            "uq_branch_bindings_active_workspace_name",
            "frontend_workspace_name",
            unique=True,
            postgresql_where=and_(active.is_(True), frontend_workspace_name.is_not(None)),
            sqlite_where=and_(active == 1, frontend_workspace_name.is_not(None)),
        ),
        Index("ix_branch_bindings_repo_branch", "repository_id", "branch_ref"),
    )
