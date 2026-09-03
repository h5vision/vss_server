"""Repository Tag synchronization result contracts."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class RepositoryTagSyncResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    reason: str = "REPOSITORY_TAG_SYNC_COMPLETED"
    detail: str = "Repository Tag commit 관측을 완료했습니다."
    retryable: bool = False
    repository_id: UUID
    observed_count: int
    created_count: int
    moved_count: int
    deleted_count: int
    recreated_count: int
