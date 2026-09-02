"""Repository catalog, 추적 Branch와 동기화 결과 계약."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.features.repositories.schemas import BranchRef
from backend.features.workspace_overlays.schemas import GitRevision

SyncTrigger = Literal["manual", "periodic"]
BranchChangeType = Literal["created", "fast_forward", "rewind", "deleted", "recreated"]


class RemoteBranchHead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    branch_ref: BranchRef
    commit_sha: GitRevision


class RepositoryCatalogResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[True] = True
    reason: Literal["REPOSITORY_BRANCH_CATALOG_READY"] = "REPOSITORY_BRANCH_CATALOG_READY"
    detail: str = "Repository의 원격 Branch 목록과 현재 HEAD SHA를 확인했습니다."
    retryable: Literal[False] = False
    repository_id: UUID
    default_branch_ref: BranchRef
    default_branch_exists: bool
    branches: list[RemoteBranchHead]


class TrackedBranchCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_id: UUID
    branch_ref: BranchRef
    vss_project_id: str = Field(min_length=1, max_length=255)
    tracked: bool = True

    @field_validator("vss_project_id")
    @classmethod
    def normalize_vss_project_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("vss_project_id must not be blank")
        return normalized


class TrackedBranchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    tracked_branch_id: UUID
    repository_id: UUID
    branch_ref: BranchRef
    vss_project_id: str
    current_head_sha: GitRevision | None = None
    tracked: bool
    last_fetched_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class BranchSyncOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    reason: str
    detail: str
    retryable: bool
    tracked_branch_id: UUID
    branch_ref: BranchRef
    previous_head_sha: GitRevision | None = None
    observed_head_sha: GitRevision | None = None
    change_type: BranchChangeType | None = None
    snapshot_id: UUID | None = None
    snapshot_state: str | None = None


class RepositorySyncResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    reason: str
    detail: str
    retryable: bool
    sync_run_id: UUID
    repository_id: UUID
    trigger: SyncTrigger
    state: Literal["succeeded", "failed"]
    started_at: datetime
    finished_at: datetime
    outcomes: list[BranchSyncOutcome]
