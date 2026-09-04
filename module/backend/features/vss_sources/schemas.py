"""VSS pull integration의 버전 고정 응답 계약."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.core.orchestration import IndexOrchestrationMode
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


class VssPullCapabilitiesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[True] = True
    schema_version: Literal["1.0"] = "1.0"
    reason: Literal["VSS_PULL_CAPABILITIES_READY"] = "VSS_PULL_CAPABILITIES_READY"
    detail: str
    retryable: Literal[False] = False
    request_id: UUID
    orchestration_mode: IndexOrchestrationMode
    index_start_owner: Literal["module", "vss"]
    module_starts_indexing: bool
    resources: list[
        Literal["source", "revisions", "refs", "context", "change_requests"]
    ]
    context_selectors: list[Literal["revision", "branch", "tag", "change_request"]]


class VssSnapshotReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: UUID | None = None
    snapshot_state: str | None = None
    materialized: bool = False
    source_ready: bool = False
    vss_state: str | None = None
    index_ready_observed: bool = False
    source_unavailable_reason: str | None = None
    index_unavailable_reason: str | None = None


class VssReferenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["branch", "tag"]
    ref: str
    revision: GitRevision
    project_id: str
    is_default: bool
    observed_at: datetime | None
    readiness: VssSnapshotReadiness


class VssReferenceListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[True] = True
    schema_version: Literal["1.0"] = "1.0"
    reason: Literal["VSS_REFS_READY"] = "VSS_REFS_READY"
    detail: str
    retryable: Literal[False] = False
    request_id: UUID
    project_id: str
    repository_id: UUID
    repository_name: str
    orchestration_mode: IndexOrchestrationMode
    items: list[VssReferenceItem]


class VssContextSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["revision", "branch", "tag", "change_request"]
    value: str
    role: Literal["base", "head", "merge"] | None = None
    reason: Literal[
        "EXACT_REVISION",
        "BRANCH_HEAD",
        "TAG_TARGET",
        "CHANGE_REQUEST_BASE",
        "CHANGE_REQUEST_HEAD",
        "CHANGE_REQUEST_MERGE",
    ]


class VssCommitContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commit_sha: GitRevision
    tree_sha: GitRevision
    parent_shas: list[GitRevision]
    author_name: str | None
    authored_at: datetime
    committed_at: datetime
    subject: str


class VssContextResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[True] = True
    schema_version: Literal["1.0"] = "1.0"
    reason: Literal["VSS_CONTEXT_READY"] = "VSS_CONTEXT_READY"
    detail: str
    retryable: Literal[False] = False
    request_id: UUID
    project_id: str
    repository_id: UUID
    repository_name: str
    orchestration_mode: IndexOrchestrationMode
    selection: VssContextSelection
    selected_revision: GitRevision
    commit: VssCommitContext | None
    readiness: VssSnapshotReadiness


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
