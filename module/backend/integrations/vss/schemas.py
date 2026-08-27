"""Schemas for the pinned h5vision/vss_server HTTP boundary."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

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
    """Supported subset of vss.config.Config fingerprint overrides."""

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


class VssIndexRequest(BaseModel):
    """Exact JSON body accepted by ``POST /index`` at the pinned VSS SHA."""

    model_config = ConfigDict(extra="forbid")

    project_root: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    profile: VssIndexProfile | None = None
    force: bool = False
    briefing: bool = True
    note: str | None = None

    @field_validator("project_root", "project_id", "note")
    @classmethod
    def strip_non_blank_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized


class VssIndexSubmission(BaseModel):
    """Backend-only metadata kept out of the VSS HTTP body."""

    model_config = ConfigDict(extra="forbid")

    request: VssIndexRequest
    expected_revision: GitRevision
    snapshot_id: str = Field(min_length=1)

    @field_validator("snapshot_id")
    @classmethod
    def strip_snapshot_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized


class VssStartIndexResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    accepted: bool
    project_id: str | None = None
    state: VssIndexState | None = None
    reason: str | None = None
    path: str | None = None
    heartbeat_age_s: float | None = Field(default=None, ge=0)
    fingerprint: dict[str, Any] | None = None


class VssStartIndexResponse(BaseModel):
    """Preserve the upstream status because 202 and 409 have different meanings."""

    model_config = ConfigDict(extra="forbid")

    status_code: Literal[202, 409]
    result: VssStartIndexResult


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
    use_bm25: bool | None = None
    context_header: bool | None = None
    chunker: str | None = None
    note: str | None = None
    briefing: Any | None = None


class VssProjectsResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    projects: list[VssProject] = Field(default_factory=list)
    incomplete: list[dict[str, Any]] = Field(default_factory=list)


class VssExistsResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    project_id: str
    exists: bool
    chunks: int = Field(default=0, ge=0)
    commit: GitRevision | None = None


class VssHealthResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: Literal[True]
    store: str
    ollama: str
    chat_model: str
    embed_model: str
    projects: list[str] = Field(default_factory=list)
    incomplete: list[dict[str, Any]] = Field(default_factory=list)
    project_aliases: dict[str, str] = Field(default_factory=dict)
    defaults: dict[str, Any] = Field(default_factory=dict)


class VssModelsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[str] = Field(default_factory=list)
    default: str


class VssBriefingResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: Literal[True]
    project_id: str
    index_id: str | None = None
    briefing: str
    model: str | None = None
    commit: GitRevision | None = None
    generated_at: str | None = None
