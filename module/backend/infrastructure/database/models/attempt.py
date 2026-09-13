"""SnapshotAttempt ORM model."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
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
    from backend.infrastructure.database.models.snapshot import Snapshot


class SnapshotAttempt(Base):
    __tablename__ = "snapshot_attempts"

    attempt_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("snapshots.snapshot_id", ondelete="RESTRICT"),
        nullable=False,
    )
    request_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    upstream_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vss_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vss_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vss_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    vss_result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Relationships
    snapshot: Mapped[Snapshot] = relationship(
        "Snapshot",
        back_populates="attempts",
    )

    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "attempt_number",
            name="uq_snapshot_attempts_snapshot_number",
        ),
        CheckConstraint("attempt_number >= 1", name="ck_snapshot_attempts_number"),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_snapshot_attempts_latency",
        ),
        CheckConstraint(
            "upstream_status_code IS NULL OR upstream_status_code BETWEEN 100 AND 599",
            name="ck_snapshot_attempts_status_code",
        ),
    )
