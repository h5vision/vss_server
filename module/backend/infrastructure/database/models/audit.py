"""AuditLog ORM model for admin mutations and access tracking."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.infrastructure.database.base import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    audit_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    request_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
    )
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(255), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, default="succeeded")
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('succeeded', 'failed', 'denied')",
            name="ck_audit_logs_outcome",
        ),
        Index("ix_audit_logs_target", "target_type", "target_id"),
        Index("ix_audit_logs_created_at", "created_at"),
    )
