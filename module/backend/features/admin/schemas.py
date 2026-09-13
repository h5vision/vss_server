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

    tracked: bool | None = None

    @model_validator(mode="after")
    def require_a_change(self) -> TrackedBranchAdminUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("at least one field must be supplied")
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
    lease_generation: int = Field(default=1, ge=1)
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
    head_commit: GitRevision | None = None
    chunks: int | None = Field(default=None, ge=0)
    indexed_at: str | None = None
    dirty: bool | None = None
    stale: bool | None = None
    current: bool | None = None
    chunker: str | None = None
    use_bm25: bool | None = None
    bm25_docs: int | None = Field(default=None, ge=0)
    context_header: bool | None = None
    briefing_status: str | None = None


class AdminVssProjectsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AdminVssProjectItem]


class AdminVssIndexedFileItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    type: str | None = None
    chunks: int | None = Field(default=None, ge=0)
    line_max: int | None = Field(default=None, ge=0)
    symbols: list[str] = Field(default_factory=list)


class AdminVssProjectContentsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    index_id: str
    resolved_by: str | None = None
    candidates: list[str] = Field(default_factory=list)
    files: list[AdminVssIndexedFileItem] = Field(default_factory=list)


class AdminVssBriefingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[True] = True
    project_id: str
    index_id: str
    briefing: str
    model: str | None = None
    commit: GitRevision | None = None
    generated_at: str | None = None
    quality_status: str | None = None
    run_id: str | None = None
    pipeline_version: str | None = None
    structure: dict[str, Any] = Field(default_factory=dict)
    routes: list[dict[str, Any]] = Field(default_factory=list)
    topics: list[dict[str, Any]] = Field(default_factory=list)
    problems: list[dict[str, Any]] = Field(default_factory=list)
    coverage: dict[str, Any] | None = None
    metrics: list[dict[str, Any]] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    references: list[dict[str, Any]] = Field(default_factory=list)
    reference_files: list[dict[str, Any]] = Field(default_factory=list)
    rag: dict[str, Any] | None = None
    cited: list[int] = Field(default_factory=list)
    truncated: list[dict[str, Any]] = Field(default_factory=list)


class AdminVssBriefingStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    index_id: str
    state: Literal["none", "queued", "running", "ready", "failed"]
    stage: str | None = None
    run_id: str | None = None
    calls: Any | None = None
    reason: str | None = None
    cleanup: Any | None = None
    elapsed_s: float | None = Field(default=None, ge=0)
    quality_status: str | None = None
    problems: list[dict[str, Any]] = Field(default_factory=list)
    updated_at: str | None = None


class AdminVssRequestFailureItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_id: UUID
    request_id: UUID
    created_at: datetime
    method: str
    path: str
    status_code: int = Field(ge=100, le=599)
    project_id: str | None = None
    reason: str
    outcome: Literal["failed", "denied"]
    query: dict[str, Any] = Field(default_factory=dict)


class AdminVssRequestFailureListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AdminVssRequestFailureItem]
    next_cursor: str | None = None


class AdminRuntimeModelsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[True] = True
    available: bool
    models: list[str]
    installed_models: list[str]
    stopped_models: list[str]
    auto_up_models: list[str]


class AdminRuntimeModelControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=255)

    @field_validator("model")
    @classmethod
    def normalize_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("model must not be empty")
        return normalized


class AdminRuntimeModelRunRequest(AdminRuntimeModelControlRequest):
    """Backward-compatible alias for the original `/run` contract."""


class AdminRuntimeModelRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[True] = True
    model: str
    already_running: bool
    models: list[str]
    auto_up_models: list[str]


class AdminRuntimeModelDownResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[True] = True
    model: str
    already_stopped: bool
    auto_up_disabled: bool
    models: list[str]
    auto_up_models: list[str]


class AdminRuntimeModelReloadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[True] = True
    model: str
    was_running: bool
    models: list[str]
    auto_up_models: list[str]


class AdminRuntimeModelAutoUpRequest(AdminRuntimeModelControlRequest):
    enabled: bool


class AdminRuntimeModelAutoUpResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[True] = True
    model: str
    enabled: bool
    loaded_now: bool
    models: list[str]
    auto_up_models: list[str]


AdminCommitStatus = Literal["git_only", "materialized", "vss_indexed", "unavailable"]


class AdminCommitAssociatedRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref_type: Literal["branch"]
    name: str
    detail: str | None = None


class AdminCommitListItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commit_sha: GitRevision
    tree_sha: str
    author_name: str | None = None
    authored_at: datetime
    committed_at: datetime
    subject: str
    parent_shas: list[GitRevision]
    status: AdminCommitStatus
    snapshot_id: UUID | None = None
    snapshot_state: str | None = None
    vss_state: str | None = None
    eligible_for_answer: bool = False
    unavailable_reason: str | None = None
    associated_refs: list[AdminCommitAssociatedRef] = Field(default_factory=list)


class AdminCommitListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[True] = True
    items: list[AdminCommitListItem]
    next_cursor: str | None = None
    total_count: int | None = None


class AdminCommitDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[True] = True
    commit: AdminCommitListItem


class AdminCommitCompareChangeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    change_type: Literal["added", "modified", "deleted", "renamed", "copied"]
    old_path: str | None = None


class AdminCommitCompareResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[True] = True
    repository_id: UUID
    base_revision: GitRevision
    target_revision: GitRevision
    merge_base_revision: GitRevision | None = None
    ahead_count: int
    behind_count: int
    files_changed: int
    additions: int
    deletions: int
    changes: list[AdminCommitCompareChangeItem]
    base_status: AdminCommitStatus
    target_status: AdminCommitStatus


class AdminCommitMaterializeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vss_project_id: str | None = Field(default=None, max_length=255)
    branch_ref: str | None = Field(default=None, max_length=512)


class AdminCommitMaterializeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[True] = True
    repository_id: UUID
    commit_sha: GitRevision
    snapshot_id: UUID
    state: str
    vss_project_id: str
    materialized_locator: str | None = None
    created: bool
