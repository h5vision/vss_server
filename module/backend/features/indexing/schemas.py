"""서버 내부정보를 노출하지 않는 Frontend 인덱싱 상태 계약."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.features.snapshots.schemas import SnapshotState
from backend.features.workspace_overlays.schemas import GitRevision


class VssProgressResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str
    processed: int | None = Field(default=None, ge=0)
    total: int | None = Field(default=None, ge=0)
    chunk_count: int | None = Field(default=None, ge=0)


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

    examined: int = Field(ge=0)
    synchronized: int = Field(ge=0)
    unavailable: int = Field(ge=0)
    failed: int = Field(ge=0)
