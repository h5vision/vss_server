"""수집 코어 내부 API의 요청·응답 규약."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TrackedBranchCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branch_ref: str = Field(min_length=1, max_length=512)
    vss_project_id: str = Field(min_length=1, max_length=255)


class SyncTriggerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trigger: str = Field(default="manual", pattern="^(manual|startup)$")


class TrackedBranchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    tracked_branch_id: UUID
    repository_id: UUID
    branch_ref: str
    vss_project_id: str
    tracked: bool
    current_head_sha: str | None
    last_fetched_at: datetime | None
    created_at: datetime


class BranchCatalogEntry(BaseModel):
    branch_ref: str
    head_sha: str
    tracked: bool = False
    vss_project_id: str | None = None
    tracked_branch_id: UUID | None = None
    latest_snapshot_state: str | None = None


class BranchCatalogResponse(BaseModel):
    repository_id: UUID
    branches: list[BranchCatalogEntry]


class BranchHeadHistoryEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    history_id: UUID
    tracked_branch_id: UUID
    previous_head_sha: str | None
    observed_head_sha: str | None
    change_type: str
    observed_at: datetime


class TrackedBranchHistoryResponse(BaseModel):
    tracked_branch_id: UUID
    branch_ref: str
    vss_project_id: str
    tracked: bool
    current_head_sha: str | None
    last_fetched_at: datetime | None
    history: list[BranchHeadHistoryEntry]


class BranchListResponse(BaseModel):
    repository_id: UUID
    branches: list[TrackedBranchResponse]


class SyncRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sync_run_id: UUID
    repository_id: UUID
    trigger: str
    state: str
    reason: str | None
    detail: str | None
    started_at: datetime
    finished_at: datetime | None


class SyncRunListResponse(BaseModel):
    repository_id: UUID
    runs: list[SyncRunResponse]


class SyncSummaryResponse(BaseModel):
    ok: bool
    sync_run_id: UUID
    repository_id: UUID
    state: str
    reason: str | None
    detail: str | None
    observed_branches: int
    changed_branches: int
    snapshots_created: int
    snapshots_accepted: int
    snapshot_failures: int
