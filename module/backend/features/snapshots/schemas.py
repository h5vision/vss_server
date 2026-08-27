"""Read-only Snapshot history and retry contracts for Admin Web."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.features.repositories.schemas import BranchRef
from backend.features.workspace_overlays.schemas import GitRevision


class SnapshotState(StrEnum):
    RECEIVED = "received"
    VALIDATED = "validated"
    BINDING_REQUIRED = "binding_required"
    MATERIALIZING = "materializing"
    MATERIALIZED = "materialized"
    SUBMITTING = "submitting"
    ACCEPTED = "accepted"
    INDEXING = "indexing"
    ALREADY_INDEXED = "already_indexed"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"


class SnapshotSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: UUID
    request_id: UUID
    binding_id: UUID
    frontend_project_id: str
    repository_id: UUID
    branch_ref: BranchRef
    vss_project_id: str
    base_revision: GitRevision
    target_revision: GitRevision
    source_type: str
    state: SnapshotState
    attempt_count: int = Field(ge=0)
    materialized_project_root: str | None = None
    vss_state: str | None = None
    vss_reason: str | None = None
    vss_detail: str | None = None
    created_at: datetime
    updated_at: datetime


class SnapshotAttemptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: UUID
    snapshot_id: UUID
    request_id: UUID
    attempt_number: int = Field(ge=1)
    started_at: datetime
    finished_at: datetime | None = None
    vss_state: str | None = None
    vss_reason: str | None = None
    vss_detail: str | None = None
    retryable: bool | None = None
    latency_ms: float | None = Field(default=None, ge=0)
    module_result_json: dict[str, Any] | None = None


class SnapshotDetailResponse(SnapshotSummaryResponse):
    attempts: list[SnapshotAttemptResponse]
    changed_file_count: int = Field(ge=0)
    deleted_path_count: int = Field(ge=0)
    rename_count: int = Field(ge=0)


class SnapshotListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[SnapshotSummaryResponse]
    next_cursor: str | None = None


class SnapshotRetryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[True] = True
    reason: str
    detail: str
    snapshot_id: UUID
    state: SnapshotState
    attempt_count: int = Field(ge=1)
