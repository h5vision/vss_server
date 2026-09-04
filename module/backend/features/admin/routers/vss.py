"""Admin VSS integration routes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from backend.core.errors import ApiError
from backend.features.admin.common import (
    Viewer,
)
from backend.features.admin.schemas import (
    AdminVssProjectItem,
    AdminVssProjectsResponse,
)
from backend.integrations.vss.errors import VssIntegrationError

router = APIRouter()


@router.get("/vss/projects", response_model=AdminVssProjectsResponse)
async def list_vss_projects(request: Request, _identity: Viewer) -> AdminVssProjectsResponse:
    try:
        response = await run_in_threadpool(request.app.state.vss_client.list_projects)
    except VssIntegrationError as exc:
        raise ApiError(
            status_code=503 if exc.retryable else 502,
            reason=exc.reason,
            detail="VSS project catalog is unavailable.",
            retryable=exc.retryable,
        ) from exc
    return AdminVssProjectsResponse(
        items=[
            AdminVssProjectItem(
                project_id=item.project_id,
                state=item.state.value,
                commit=item.commit,
                chunks=item.chunks,
                indexed_at=item.indexed_at,
            )
            for item in response.projects
        ]
    )
