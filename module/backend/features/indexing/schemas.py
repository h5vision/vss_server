"""서버 내부정보를 노출하지 않는 Frontend 인덱싱 상태 계약."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.features.snapshots.schemas import SnapshotState
from backend.features.workspace_overlays.schemas import GitRevision


class IncrementalProgressResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changed_files: int = Field(ge=0)
    deleted_files: int = Field(ge=0)
    unchanged_files: int = Field(ge=0)
    reused_chunks: int = Field(ge=0)
    rebuilt_chunks: int = Field(ge=0)


class VssProgressResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str
    index_id: str | None = None
    index_matches_project: bool | None = None
    mode: Literal["full", "incremental"] | None = None
    processed: int | None = Field(default=None, ge=0)
    total: int | None = Field(default=None, ge=0)
    chunk_count: int | None = Field(default=None, ge=0)
    active_revision: GitRevision | None = None
    revision_matches: bool | None = None
    source_matches_snapshot: bool | None = None
    dirty: bool | None = None
    bm25_count: int | None = Field(default=None, ge=0)
    incremental: IncrementalProgressResponse | None = None
    briefing: str | None = None
    briefing_error: str | None = None
    elapsed_s: float | None = Field(default=None, ge=0)


class IndexStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[True] = True
    reason: str
    detail: str
    retryable: bool
    request_id: UUID
    snapshot_id: UUID
    project_id: str
    state: SnapshotState
    target_revision: GitRevision
    vss: VssProgressResponse


class RecoverySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lock_acquired: bool
    examined: int = Field(ge=0)
    synchronized: int = Field(ge=0)
    unavailable: int = Field(ge=0)
    failed: int = Field(ge=0)
