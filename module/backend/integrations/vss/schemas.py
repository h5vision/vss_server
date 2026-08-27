"""Schemas for the current h5vision/vss_server module boundary."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.features.workspace_overlays.schemas import GitRevision


class VssIndexState(StrEnum):
    NONE = "none"
    RUNNING = "running"
    INDEXING_LEXICAL = "indexing_lexical"
    PROMOTING = "promoting"
    DONE = "done"
    FAILED = "failed"
    ABORTED = "aborted"


class VssIndexProfile(BaseModel):
    """Supported subset of vss.config.Config.fingerprint overrides."""

    model_config = ConfigDict(extra="forbid")

    embed_model: str | None = None
    chunker: str | None = None
    chunk_size: int | None = Field(default=None, gt=0)
    chunk_overlap: int | None = Field(default=None, ge=0)
    min_chunk_chars: int | None = Field(default=None, ge=0)
    ast_max_chars: int | None = Field(default=None, gt=0)
    max_file_bytes: int | None = Field(default=None, gt=0)
    context_header: bool | None = None
    use_bm25: bool | None = None
    exclude_globs: str | None = None


class VssIndexCommand(BaseModel):
    """Backend command; expected_revision is verified, not passed as a fake VSS argument."""

    model_config = ConfigDict(extra="forbid")

    project_root: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    expected_revision: GitRevision
    snapshot_id: str = Field(min_length=1, max_length=512)
    profile: VssIndexProfile | None = None
    force: bool = False

    @field_validator("project_root", "project_id", "snapshot_id")
    @classmethod
    def strip_non_blank_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    def start_index_kwargs(self) -> dict[str, Any]:
        """Return only arguments accepted by vss.indexer.start_index at the pinned SHA."""

        return {
            "project_root": self.project_root,
            "project_id": self.project_id,
            "profile": self.profile.model_dump(exclude_none=True) if self.profile else None,
            "blocking": False,
            "force": self.force,
            "extra_meta": {
                "snapshot_id": self.snapshot_id,
                "requested_revision": self.expected_revision,
            },
        }


class VssStartIndexResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    accepted: bool
    project_id: str | None = None
    state: VssIndexState | None = None
    reason: str | None = None
    path: str | None = None
    heartbeat_age_s: float | None = Field(default=None, ge=0)
    fingerprint: dict[str, Any] | None = None


class VssIndexInfo(BaseModel):
    model_config = ConfigDict(extra="allow")

    chunks: int | None = Field(default=None, ge=0)
    commit: GitRevision | None = None
    fingerprint: dict[str, Any] | None = None
    indexed_at: str | None = None
    project_root: str | None = None
    bm25_count: int | None = Field(default=None, ge=0)


class VssIndexStatus(BaseModel):
    model_config = ConfigDict(extra="allow")

    project_id: str
    state: VssIndexState
    processed: int | None = Field(default=None, ge=0)
    total: int | None = Field(default=None, ge=0)
    chunk_count: int | None = Field(default=None, ge=0)
    error: str | None = None
    briefing: str | None = None
    index: VssIndexInfo | None = None
    incomplete: list[dict[str, Any]] = Field(default_factory=list)

    def completed_for(self, revision: str) -> bool:
        return (
            self.state is VssIndexState.DONE
            and self.index is not None
            and self.index.commit == revision
        )


class VssProject(BaseModel):
    model_config = ConfigDict(extra="allow")

    project_id: str
    state: VssIndexState = VssIndexState.DONE
    chunks: int | None = Field(default=None, ge=0)
    commit: GitRevision | None = None
    indexed_at: str | None = None
    project_root: str | None = None
    note: str | None = None


class VssExistsResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    project_id: str
    exists: bool
    chunks: int = Field(default=0, ge=0)
    commit: GitRevision | None = None
