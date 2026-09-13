"""Safe transformations from VSS HTTP responses to Frontend contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from backend.features.frontend_proxy.schemas import (
    FrontendBriefingResponse,
    FrontendBriefingStatusResponse,
    FrontendModel,
    FrontendModelsResponse,
    FrontendProject,
    FrontendProjectsResponse,
)
from backend.integrations.vss.schemas import (
    VssBriefingResponse,
    VssBriefingStatus,
    VssModelsResponse,
    VssProjectsResponse,
)


def _briefing_status(value: Any) -> str | None:
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    if isinstance(value, dict):
        for key in ("status", "state"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def to_frontend_projects(response: VssProjectsResponse) -> FrontendProjectsResponse:
    return FrontendProjectsResponse(
        projects=[
            FrontendProject(
                project_id=project.project_id,
                name=project.project_id,
                commit=project.commit,
                head_commit=project.head_commit,
                state=project.state.value,
                chunks=project.chunks,
                indexed_at=project.indexed_at,
                note=project.note,
                dirty=project.dirty,
                stale=project.stale,
                current=project.current,
                chunker=project.chunker,
                use_bm25=project.use_bm25,
                bm25_docs=project.bm25_docs,
                context_header=project.context_header,
                briefing_status=_briefing_status(project.briefing),
            )
            for project in response.projects
        ],
        # Upstream incomplete records may contain server-local paths. Rich VSS
        # diagnostics belong to the authenticated Admin surface, not this proxy.
        incomplete=[],
    )


def to_frontend_models(response: VssModelsResponse) -> FrontendModelsResponse:
    effective_default = response.effective_default
    return FrontendModelsResponse(
        default_model_id=effective_default,
        checked_at=datetime.now(timezone.utc),
        models=[
            FrontendModel(
                model_id=model,
                model_name=model,
                display_name=model,
                is_default=model == effective_default,
            )
            for model in response.models
        ],
    )


def to_frontend_briefing(
    response: VssBriefingResponse,
    *,
    frontend_project_id: str,
    vss_project_id: str,
) -> FrontendBriefingResponse:
    return FrontendBriefingResponse(
        project_id=frontend_project_id,
        index_id=response.index_id or vss_project_id,
        briefing=response.briefing,
        model=response.model,
        commit=response.commit,
        generated_at=response.generated_at,
        quality_status=response.quality_status,
        run_id=response.run_id,
        pipeline_version=response.pipeline_version,
        structure=response.structure,
        routes=response.routes,
        topics=response.topics,
        problems=response.problems,
        coverage=response.coverage,
        metrics=response.metrics,
        materials=response.materials,
        references=response.references,
        reference_files=response.reference_files,
        rag=response.rag,
        cited=response.cited,
        truncated=response.truncated,
    )


def to_frontend_briefing_status(
    response: VssBriefingStatus,
    *,
    frontend_project_id: str,
    vss_project_id: str,
) -> FrontendBriefingStatusResponse:
    return FrontendBriefingStatusResponse(
        project_id=frontend_project_id,
        index_id=vss_project_id,
        state=response.state,
        stage=response.stage,
        run_id=response.run_id,
        calls=response.calls,
        reason=response.reason,
        cleanup=response.cleanup,
        elapsed_s=response.elapsed_s,
        quality_status=response.quality_status,
        problems=response.problems,
        updated_at=response.updated_at,
    )
