"""Response contracts consumed by the pinned Vision frontend handlers."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.features.workspace_overlays.schemas import GitRevision


class FrontendProject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    name: str
    commit: GitRevision | None = None
    head_commit: GitRevision | None = None
    state: str
    chunks: int | None = Field(default=None, ge=0)
    indexed_at: str | None = None
    note: str | None = None
    dirty: bool | None = None
    stale: bool | None = None
    current: bool | None = None
    chunker: str | None = None
    use_bm25: bool | None = None
    bm25_docs: int | None = Field(default=None, ge=0)
    context_header: bool | None = None
    briefing_status: str | None = None


class FrontendProjectsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    projects: list[FrontendProject]
    incomplete: list[dict]


class FrontendModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str
    model_name: str
    display_name: str
    provider: str = "ollama"
    location: str = "vss_server"
    deployment_type: str = "server"
    endpoint: str = ""
    enabled: bool = True
    available: bool = True
    is_default: bool
    streaming: bool = True


class FrontendModelsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    default_model_id: str | None = None
    checked_at: datetime
    models: list[FrontendModel]


class FrontendBriefingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    project_id: str
    index_id: str
    briefing: str
    model: str | None = None
    commit: GitRevision | None = None
    generated_at: str | None = None
    quality_status: str | None = None
    run_id: str | None = None
    pipeline_version: str | None = None
    structure: dict[str, Any] = Field(default_factory=dict)
    routes: list[dict[str, Any]] = Field(default_factory=list)
    topics: list[dict[str, Any]] = Field(default_factory=list)
    problems: list[dict[str, Any]] = Field(default_factory=list)
    coverage: dict[str, Any] | None = None
    metrics: list[dict[str, Any]] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    references: list[dict[str, Any]] = Field(default_factory=list)
    reference_files: list[dict[str, Any]] = Field(default_factory=list)
    rag: dict[str, Any] | None = None
    cited: list[int] = Field(default_factory=list)
    truncated: list[dict[str, Any]] = Field(default_factory=list)


class FrontendBriefingStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[True] = True
    project_id: str
    index_id: str
    state: Literal["none", "queued", "running", "ready", "failed"]
    stage: str | None = None
    run_id: str | None = None
    calls: Any | None = None
    reason: str | None = None
    cleanup: Any | None = None
    elapsed_s: float | None = Field(default=None, ge=0)
    quality_status: str | None = None
    problems: list[dict[str, Any]] = Field(default_factory=list)
    updated_at: str | None = None
