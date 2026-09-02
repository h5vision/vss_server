"""Read-only Snapshot history and retry contracts for Admin Web."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.features.repositories.schemas import BranchRef
from backend.features.workspace_overlays.schemas import GitRevision


class SnapshotState(str, Enum):
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
    ABORTED = "aborted"


class SnapshotSourceType(str, Enum):
    """How the base tree was obtained for materialization."""

    CLIENT_LOCAL_GIT = "client_local_git"
    REMOTE_CLONE = "remote_clone"
    PRIOR_REVISION = "prior_revision"
    BOOTSTRAP_FULL = "bootstrap_full"


class SnapshotSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: UUID
    request_id: UUID
    binding_id: UUID | None = None
    tracked_branch_id: UUID | None = None
    frontend_project_id: str | None = None
    repository_id: UUID
    branch_ref: BranchRef
    vss_project_id: str
    base_revision: GitRevision
    target_revision: GitRevision
    source_type: SnapshotSourceType
    state: SnapshotState
    attempt_count: int = Field(ge=0)
    materialized_locator: str | None = None
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
    upstream_status_code: int | None = None
    vss_state: str | None = None
    vss_reason: str | None = None
    vss_detail: str | None = None
    retryable: bool | None = None
    latency_ms: float | None = Field(default=None, ge=0)
    vss_result_json: dict[str, Any] | None = None


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
    retryable: bool
    request_id: UUID
    snapshot_id: UUID
    state: SnapshotState
    attempt_count: int = Field(ge=0)
