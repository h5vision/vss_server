"""Provider-neutral PR/MR observation contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.features.repositories.schemas import BranchRef
from backend.features.workspace_overlays.schemas import GitRevision

ChangeRequestProvider = Literal["github", "gitlab"]
ChangeRequestKind = Literal["pull_request", "merge_request"]
ChangeRequestState = Literal["open", "closed", "merged"]


class ChangeRequestObservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_id: UUID
    provider: ChangeRequestProvider
    external_number: int = Field(gt=0)
    kind: ChangeRequestKind
    state: ChangeRequestState
    title: str | None = Field(default=None, max_length=512)
    base_ref: BranchRef
    head_ref: BranchRef
    base_sha: GitRevision
    head_sha: GitRevision
    merge_sha: GitRevision | None = None
    provider_updated_at: datetime | None = None
    merged_at: datetime | None = None
    observed_at: datetime

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_provider_and_merge_state(self) -> ChangeRequestObservationRequest:
        expected_kind = "pull_request" if self.provider == "github" else "merge_request"
        if self.kind != expected_kind:
            raise ValueError(f"{self.provider} change request kind must be {expected_kind}")
        if self.state == "merged":
            if self.merge_sha is None or self.merged_at is None:
                raise ValueError("merged change request requires merge_sha and merged_at")
        elif self.merge_sha is not None or self.merged_at is not None:
            raise ValueError("non-merged change request must not expose merge revision")
        return self


class ChangeRequestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    change_request_id: UUID
    repository_id: UUID
    provider: ChangeRequestProvider
    external_number: int
    kind: ChangeRequestKind
    state: ChangeRequestState
    title: str | None
    base_ref: BranchRef
    head_ref: BranchRef
    current_base_sha: GitRevision
    current_head_sha: GitRevision
    current_merge_sha: GitRevision | None
    last_observed_at: datetime
    provider_updated_at: datetime | None
    merged_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ChangeRequestRevisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    revision_observation_id: UUID
    change_request_id: UUID
    state: ChangeRequestState
    base_ref: BranchRef
    head_ref: BranchRef
    base_sha: GitRevision
    head_sha: GitRevision
    merge_sha: GitRevision | None
    provider_updated_at: datetime | None
    observed_at: datetime
