"""Response contracts consumed by the pinned Vision frontend handlers."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from backend.features.workspace_overlays.schemas import GitRevision


class FrontendProject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    name: str
    commit: GitRevision | None = None
    state: str
    chunks: int | None = Field(default=None, ge=0)
    indexed_at: str | None = None
    note: str | None = None


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
    default_model_id: str
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
