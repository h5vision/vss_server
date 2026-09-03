"""Repository ORM model."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.infrastructure.database.base import Base

if TYPE_CHECKING:
    from backend.infrastructure.database.models.binding import BranchBinding
    from backend.infrastructure.database.models.change_request import ChangeRequest
    from backend.infrastructure.database.models.collection import RepositorySyncRun, TrackedBranch
    from backend.infrastructure.database.models.commit_catalog import (
        CommitCatalogRun,
        RepositoryCommit,
    )
    from backend.infrastructure.database.models.snapshot import Snapshot
    from backend.infrastructure.database.models.tag import RepositoryTag


class Repository(Base):
    __tablename__ = "repositories"

    repository_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    canonical_name: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    remote_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    default_branch_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
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
    branch_bindings: Mapped[list[BranchBinding]] = relationship(
        "BranchBinding",
        back_populates="repository",
    )
    snapshots: Mapped[list[Snapshot]] = relationship(
        "Snapshot",
        back_populates="repository",
    )
    tracked_branches: Mapped[list[TrackedBranch]] = relationship(
        "TrackedBranch",
        back_populates="repository",
    )
    sync_runs: Mapped[list[RepositorySyncRun]] = relationship(
        "RepositorySyncRun",
        back_populates="repository",
    )
    change_requests: Mapped[list[ChangeRequest]] = relationship(
        "ChangeRequest",
        back_populates="repository",
    )
    commits: Mapped[list[RepositoryCommit]] = relationship(
        "RepositoryCommit",
        back_populates="repository",
    )
    commit_catalog_runs: Mapped[list[CommitCatalogRun]] = relationship(
        "CommitCatalogRun",
        back_populates="repository",
    )
    tags: Mapped[list[RepositoryTag]] = relationship(
        "RepositoryTag",
        back_populates="repository",
    )
