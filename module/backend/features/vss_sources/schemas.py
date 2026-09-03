"""VSS pull integration의 버전 고정 응답 계약."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.features.change_requests.schemas import (
    ChangeRequestKind,
    ChangeRequestProvider,
    ChangeRequestState,
)
from backend.features.repositories.schemas import BranchRef
from backend.features.workspace_overlays.schemas import GitRevision
from backend.integrations.vss.schemas import VssIndexRequest


class GitSourceVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_commit_sha: GitRevision
    expected_tree_sha: GitRevision
    object_format: Literal["sha1"]
    git_metadata_present: Literal[True]
    working_tree_clean: Literal[True]
    verified_at: datetime
    verification_commands: list[str] = Field(min_length=3, max_length=3)


class VssSourceDescriptorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[True] = True
    schema_version: Literal["1.0"] = "1.0"
    reason: Literal["VSS_SOURCE_READY"] = "VSS_SOURCE_READY"
    detail: str
    retryable: Literal[False] = False
    request_id: UUID
    project_id: str
    repository_id: UUID
    repository_name: str
    branch_ref: BranchRef
    snapshot_id: UUID
    snapshot_state: str
    source_type: str
    base_revision: GitRevision
    target_revision: GitRevision
    verification: GitSourceVerification
    index_request: VssIndexRequest


class VssRevisionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: UUID
    repository_id: UUID
    branch_ref: BranchRef
    base_revision: GitRevision
    target_revision: GitRevision
    snapshot_state: str
    materialized: bool
    vss_state: str | None = None
    created_at: datetime
    updated_at: datetime


class VssRevisionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[True] = True
    schema_version: Literal["1.0"] = "1.0"
    reason: Literal["VSS_REVISION_HISTORY_READY"] = "VSS_REVISION_HISTORY_READY"
    detail: str
    retryable: Literal[False] = False
    request_id: UUID
    project_id: str
    items: list[VssRevisionItem]
    next_cursor: str | None = None


class VssRevisionAvailability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["base", "head", "merge"]
    revision: GitRevision
    snapshot_id: UUID | None = None
    snapshot_state: str | None = None
    vss_state: str | None = None
    eligible_for_answer: bool
    unavailable_reason: str | None = None


class VssChangeRequestItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    change_request_id: UUID
    repository_id: UUID
    provider: ChangeRequestProvider
    external_number: int
    kind: ChangeRequestKind
    state: ChangeRequestState
    title: str | None
    base_ref: BranchRef
    head_ref: BranchRef
    base_sha: GitRevision
    head_sha: GitRevision
    merge_sha: GitRevision | None
    last_observed_at: datetime
    provider_updated_at: datetime | None
    merged_at: datetime | None
    revisions: list[VssRevisionAvailability]


class VssChangeRequestRevisionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_observation_id: UUID
    state: ChangeRequestState
    base_ref: BranchRef
    head_ref: BranchRef
    base_sha: GitRevision
    head_sha: GitRevision
    merge_sha: GitRevision | None
    provider_updated_at: datetime | None
    observed_at: datetime


class VssChangeRequestListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[True] = True
    schema_version: Literal["1.0"] = "1.0"
    reason: Literal["VSS_CHANGE_REQUESTS_READY"] = "VSS_CHANGE_REQUESTS_READY"
    detail: str
    retryable: Literal[False] = False
    request_id: UUID
    project_id: str
    items: list[VssChangeRequestItem]
    next_cursor: str | None = None


class VssChangeRequestDetailResponse(VssChangeRequestItem):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[True] = True
    schema_version: Literal["1.0"] = "1.0"
    reason: Literal["VSS_CHANGE_REQUEST_READY"] = "VSS_CHANGE_REQUEST_READY"
    detail: str
    retryable: Literal[False] = False
    request_id: UUID
    project_id: str
    observations: list[VssChangeRequestRevisionItem]
