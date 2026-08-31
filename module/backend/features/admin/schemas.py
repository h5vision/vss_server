"""Common schemas for the authenticated Admin Web boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from backend.features.repositories.schemas import BranchRef


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


class AuditLogResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_id: UUID
    request_id: UUID
    actor: str
    action: str
    target_type: str
    target_id: str
    outcome: str
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


class TrackedBranchAdminCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_id: UUID
    branch_ref: BranchRef
    vss_project_id: str | None = None
    tracked: bool = True

    @field_validator("vss_project_id")
    @classmethod
    def strip_vss_project_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        return normalized


class TrackedBranchAdminUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vss_project_id: str | None = Field(default=None, min_length=1)
    tracked: bool | None = None

    @field_validator("vss_project_id")
    @classmethod
    def strip_vss_project_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @model_validator(mode="after")
    def require_at_least_one_change(self) -> TrackedBranchAdminUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("at least one field must be supplied")
        return self


class TrackedBranchAdminResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tracked_branch_id: UUID
    repository_id: UUID
    branch_ref: BranchRef
    vss_project_id: str
    tracked: bool
    current_head_sha: str | None = None
    last_fetched_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class TrackedBranchAdminListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[TrackedBranchAdminResponse]
    next_cursor: str | None = None


class BranchHeadHistoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    history_id: UUID
    tracked_branch_id: UUID
    previous_head_sha: str | None = None
    observed_head_sha: str | None = None
    change_type: str
    sync_run_id: UUID | None = None
    observed_at: datetime


class BranchHeadHistoryListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[BranchHeadHistoryItem]
    next_cursor: str | None = None


class RepositorySyncRunItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sync_run_id: UUID
    repository_id: UUID
    trigger: str
    state: str
    reason: str | None = None
    detail: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


class RepositorySyncRunListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RepositorySyncRunItem]
    next_cursor: str | None = None


class AdminVssProjectItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    project_id: str
    active: bool | None = None
    state: str | None = None
    commit: str | None = None
    chunks: int | None = None
    indexed_at: str | None = None


class AdminVssProjectsResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    items: list[AdminVssProjectItem]
