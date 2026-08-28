"""SnapshotDelta ORM model."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.infrastructure.database.base import Base

if TYPE_CHECKING:
    from backend.infrastructure.database.models.snapshot import Snapshot


class SnapshotDelta(Base):
    __tablename__ = "snapshot_deltas"

    delta_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("snapshots.snapshot_id", ondelete="RESTRICT"),
        nullable=False,
    )
    # added, modified, deleted, renamed
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    path: Mapped[str] = mapped_column(String(4096), nullable=False)
    old_path: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    encoding: Mapped[str] = mapped_column(String(32), default="utf-8", nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_locator: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships
    snapshot: Mapped[Snapshot] = relationship(
        "Snapshot",
        back_populates="deltas",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('added', 'modified', 'deleted', 'renamed')",
            name="ck_snapshot_deltas_status",
        ),
        CheckConstraint("encoding = 'utf-8'", name="ck_snapshot_deltas_encoding"),
        CheckConstraint(
            "NOT (content IS NOT NULL AND content_locator IS NOT NULL)",
            name="ck_snapshot_deltas_content_storage",
        ),
        Index("ix_snapshot_deltas_snapshot_id", "snapshot_id"),
    )
