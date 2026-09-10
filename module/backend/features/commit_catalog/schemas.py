"""Commit graph scanner and catalog result contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.features.workspace_overlays.schemas import GitRevision

MissingParentReason = Literal["scan_truncated", "shallow_history", "object_unavailable"]


class CommitGraphEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    commit_sha: GitRevision
    tree_sha: GitRevision
    parent_shas: list[GitRevision]
    author_name: str | None = Field(default=None, max_length=255)
    authored_at: datetime
    committed_at: datetime
    subject: str = Field(max_length=512)


class CommitGraphScanResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    roots: list[GitRevision]
    unavailable_roots: list[GitRevision]
    entries: list[CommitGraphEntry]
    truncated: bool
    shallow: bool
    history_complete: bool


class CommitCatalogResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    reason: str
    detail: str
    retryable: bool
    run_id: UUID
    repository_id: UUID
    roots: list[GitRevision]
    unavailable_roots: list[GitRevision]
    discovered_count: int
    persisted_count: int
    truncated: bool
    shallow: bool
    history_complete: bool
    started_at: datetime
    finished_at: datetime
