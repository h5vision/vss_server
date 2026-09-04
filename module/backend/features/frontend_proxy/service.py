"""Safe transformations from VSS HTTP responses to Frontend contracts."""

from __future__ import annotations

from datetime import datetime, timezone

from backend.features.frontend_proxy.schemas import (
    FrontendBriefingResponse,
    FrontendModel,
    FrontendModelsResponse,
    FrontendProject,
    FrontendProjectsResponse,
)
from backend.integrations.vss.schemas import (
    VssBriefingResponse,
    VssModelsResponse,
    VssProjectsResponse,
)


def to_frontend_projects(response: VssProjectsResponse) -> FrontendProjectsResponse:
    return FrontendProjectsResponse(
        projects=[
            FrontendProject(
                project_id=project.project_id,
                name=project.project_id,
                commit=project.commit,
                state=project.state,
                chunks=project.chunks,
                indexed_at=project.indexed_at,
                note=project.note,
            )
            for project in response.projects
        ],
        # Frontend does not consume incomplete build diagnostics and upstream
        # records may contain server-local paths. Keep the public response safe.
        incomplete=[],
    )


def to_frontend_models(response: VssModelsResponse) -> FrontendModelsResponse:
    return FrontendModelsResponse(
        default_model_id=response.default,
        checked_at=datetime.now(timezone.utc),
        models=[
            FrontendModel(
                model_id=model,
                model_name=model,
                display_name=model,
                is_default=model == response.default,
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
    )
