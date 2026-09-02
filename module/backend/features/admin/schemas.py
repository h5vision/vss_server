"""Common success and error envelopes consumed by Admin Web."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.features.repositories.schemas import BranchRef
from backend.features.workspace_overlays.schemas import GitRevision


class AdminErrorResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: Literal[False] = False
    reason: str
    detail: str
    retryable: bool
    request_id: UUID


class AdminMutationResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: Literal[True] = True
    reason: str
    detail: str
    retryable: bool = False
    request_id: UUID
    resource: dict[str, Any]


class TrackedBranchAdminUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vss_project_id: str | None = Field(default=None, min_length=1, max_length=255)
    tracked: bool | None = None

    @field_validator("vss_project_id")
    @classmethod
    def normalize_vss_project_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("vss_project_id must not be blank")
        return normalized

    @model_validator(mode="after")
    def require_a_change(self) -> TrackedBranchAdminUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("at least one field must be supplied")
        if "vss_project_id" in self.model_fields_set and self.vss_project_id is None:
            raise ValueError("vss_project_id must not be null")
        return self


class TrackedBranchAdminResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    tracked_branch_id: UUID
    repository_id: UUID
    branch_ref: BranchRef
    vss_project_id: str
    current_head_sha: GitRevision | None = None
    tracked: bool
    last_fetched_at: datetime | None = None
    latest_snapshot_state: str | None = None
    created_at: datetime
    updated_at: datetime


class TrackedBranchAdminListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[TrackedBranchAdminResponse]
    next_cursor: str | None = None


class BranchHeadHistoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    history_id: UUID
    tracked_branch_id: UUID
    sync_run_id: UUID
    previous_head_sha: GitRevision | None = None
    observed_head_sha: GitRevision | None = None
    change_type: Literal["created", "fast_forward", "rewind", "deleted", "recreated"]
    observed_at: datetime


class BranchHeadHistoryListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[BranchHeadHistoryItem]
    next_cursor: str | None = None


class RepositorySyncRunItem(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    sync_run_id: UUID
    repository_id: UUID
    trigger: Literal["manual", "periodic"]
    state: Literal["running", "succeeded", "failed"]
    reason: str
    detail: str
    retryable: bool
    started_at: datetime
    finished_at: datetime | None = None


class RepositorySyncRunListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RepositorySyncRunItem]
    next_cursor: str | None = None


class AuditLogResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    audit_id: UUID
    request_id: UUID
    actor: str
    action: str
    target_type: str
    target_id: str
    outcome: Literal["succeeded", "failed", "denied"]
    reason: str | None = None
    detail: str | None = None
    before_json: dict[str, Any] | None = None
    after_json: dict[str, Any] | None = None
    details: dict[str, Any] | None = None
    created_at: datetime


class AuditLogListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AuditLogResponse]
    next_cursor: str | None = None


class AdminVssProjectItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    state: str
    commit: GitRevision | None = None
    chunks: int | None = Field(default=None, ge=0)
    indexed_at: str | None = None


class AdminVssProjectsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AdminVssProjectItem]
